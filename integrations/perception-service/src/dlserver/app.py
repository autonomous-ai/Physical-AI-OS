"""DL Backend — FastAPI server.

Thin shell: app creation, lifespan (model loading), router registration.

Usage:
    python -m dlserver                  # default 0.0.0.0:8001
    python -m dlserver --port 9000      # custom port
    python -m dlserver --host 127.0.0.1 # localhost only
"""

import argparse
import asyncio
import logging
import logging.handlers
import os
import secrets
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader

from config import settings
from dlserver.routes.action import router as action_ws_router
from dlserver.routes.audio import router as audio_router
from dlserver.routes.audio_emotion import router as audio_emotion_router
from dlserver.routes.facial_emotion import http_router as emotion_http_router
from dlserver.routes.facial_emotion import ws_router as emotion_ws_router
from dlserver.routes.health import router as health_router
from dlserver.routes.object import http_router as object_http_router
from dlserver.routes.object import ws_router as object_ws_router
from dlserver.routes.pose import ws_router as pose_ws_router
from dlserver.utils.state import (
    get_action_model,
    get_audio_embedder,
    get_audio_emotion_model,
    get_emotion_model,
    get_object_models,
    get_pose_model,
    set_action_model,
    set_audio_embedder,
    set_audio_emotion_model,
    set_emotion_model,
    set_object_models,
    set_pose_model,
)
from core.livez import router as livez_router
from core.logging_ext import ResilientRotatingFileHandler, queued, uvicorn_file_log_config
from core.stackdump import install_stack_dump, stack_dump_name
from core.request_context import (
    InstanceAlreadyRunning,
    acquire_instance_lock,
    install_request_id_logging,
    request_id_middleware,
)
from factory import (
    build_action_perception,
    build_audio_embedder,
    build_audio_emotion_perception,
    build_emotion_perception,
    build_object_perceptions,
    build_pose_perception,
)

LOG_FORMAT = "%(asctime)s [%(name)s] [%(request_id)s] %(levelname)s: %(message)s"

# Must run before any record is emitted: LOG_FORMAT references %(request_id)s.
install_request_id_logging()

# Holds the single-instance flock for the process lifetime; closing it releases.
_instance_lock: object | None = None
logger = logging.getLogger(__name__)

# --- Auth ---

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def verify_api_key(api_key: str = Security(api_key_header)):
    """Validate the X-API-Key header against DL_API_KEY."""
    if not settings.dl_api_key:
        return
    if not api_key or not secrets.compare_digest(api_key, settings.dl_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# --- Lifespan ---


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load models at startup, release on shutdown."""

    # -- Action model --
    if settings.action.enabled:
        logger.info("Loading action model...")
        try:
            action_model = build_action_perception()
            await action_model.start()
            set_action_model(action_model)
            logger.info("Action model ready")
        except Exception as e:
            logger.error("Failed to load action model: %s", e)

    # -- Emotion model --
    if settings.fer.enabled:
        logger.info("Loading emotion model...")
        try:
            emotion_model = build_emotion_perception()
            await emotion_model.start()
            set_emotion_model(emotion_model)
            logger.info("Emotion model ready")
        except Exception as e:
            logger.error("Failed to load emotion model: %s", e)

    # -- Audio embedder --
    if settings.audio_embedder.enabled:
        logger.info("Loading audio embedder...")
        try:
            audio_embedder = build_audio_embedder()
            await asyncio.to_thread(audio_embedder.start)
            set_audio_embedder(audio_embedder)
            logger.info("Audio embedder ready")
        except Exception as e:
            logger.error("Failed to load audio embedder: %s", e)

    # -- Audio emotion model --
    if settings.ser.enabled:
        logger.info("Loading audio emotion model...")
        try:
            audio_emotion_model = build_audio_emotion_perception()
            await audio_emotion_model.start()
            set_audio_emotion_model(audio_emotion_model)
            logger.info("Audio emotion model ready")
        except Exception as e:
            logger.error("Failed to load audio emotion model: %s", e)

    # -- Pose estimator --
    if settings.pose.enabled:
        logger.info("Loading pose estimator...")
        try:
            pose_model = build_pose_perception()
            await pose_model.start()
            set_pose_model(pose_model)
            logger.info("Pose estimator ready")
        except Exception as e:
            logger.error("Failed to load pose estimator: %s", e)

    # -- Object detectors --
    logger.info("Loading object detectors...")
    object_models = build_object_perceptions()
    for name, model in list(object_models.items()):
        try:
            await model.start()
            logger.info("Object detector '%s' ready", name)
        except Exception as e:
            logger.error("Failed to load object detector '%s': %s", name, e)
            del object_models[name]
    set_object_models(object_models)

    yield

    logger.info("Shutting down DL backend...")
    for name, model in [
        ("action", get_action_model()),
        ("emotion", get_emotion_model()),
        ("pose", get_pose_model()),
        ("audio_emotion", get_audio_emotion_model()),
    ]:
        if model is not None:
            try:
                await model.stop()
            except Exception:
                logger.exception("Failed to stop %s", name)
    embedder = get_audio_embedder()
    if embedder is not None:
        try:
            await asyncio.to_thread(embedder.stop)
        except Exception:
            logger.exception("Failed to stop audio_embedder")
    for obj_name, obj_model in get_object_models().items():
        try:
            await obj_model.stop()
        except Exception:
            logger.exception("Failed to stop object detector '%s'", obj_name)
    logger.info("DL backend shutdown complete")


# --- App + Routers ---

app = FastAPI(title="DL Backend", lifespan=lifespan)
app.middleware("http")(request_id_middleware)
# Liveness: no prefix, no auth. Restricted to localhost at the nginx layer.
app.include_router(livez_router)

# Existing perceptions — /hal/api/dl/ prefix
app.include_router(action_ws_router, prefix="/hal/api/dl")
app.include_router(emotion_ws_router, prefix="/hal/api/dl")
app.include_router(emotion_http_router, prefix="/hal/api/dl", dependencies=[Depends(verify_api_key)])
app.include_router(health_router, prefix="/hal/api/dl", dependencies=[Depends(verify_api_key)])
app.include_router(
    audio_router, prefix="/hal/api/dl", dependencies=[Depends(verify_api_key)]
)
app.include_router(audio_emotion_router, prefix="/hal/api/dl", dependencies=[Depends(verify_api_key)])
app.include_router(pose_ws_router, prefix="/hal/api/dl")


# Object detection — /api/dl/ prefix (backward-compatible with go2)
app.include_router(object_ws_router, prefix="/api/dl")
app.include_router(object_http_router, prefix="/api/dl", dependencies=[Depends(verify_api_key)])


# --- CLI ---


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DL Backend Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8001, help="Bind port (default: 8001)")
    parser.add_argument("--log-dir", default=None, help="Directory for rotating log files")
    parser.add_argument("--pid-file", default=None, help="Write PID to this file")
    return parser.parse_args()


def _setup_logging(log_dir: str | None) -> dict[str, Any] | None:
    """Configure application logging. Returns uvicorn log_config dict (or None for console)."""
    if not log_dir:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
        return None

    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        # Hold this for the process lifetime -- see acquire_instance_lock. It must
        # be taken BEFORE the rotation below, which renames/unlinks unconditionally.
        global _instance_lock
        _instance_lock = acquire_instance_lock(log_dir)
        log_path = Path(log_dir) / "dlserver.log"
        uvicorn_log_path = Path(log_dir) / "uvicorn.log"
        # Rotate old logs
        for prefix in ("dlserver.log", "uvicorn.log"):
            for bak in Path(log_dir).glob(f"{prefix}*.bak"):
                bak.unlink()
            for old in Path(log_dir).glob(f"{prefix}*"):
                old.rename(Path(str(old) + ".bak"))
        file_handler = ResilientRotatingFileHandler(
            str(log_path), maxBytes=1_048_576, backupCount=3
        )
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        # Queued: logger.info() on the event loop only enqueues. A write that hangs
        # on the log volume blocks the writer thread, not every request (#530).
        logging.basicConfig(level=logging.INFO, handlers=[queued(file_handler)])

        # Route uvicorn/fastapi logs to a separate file (one shared, queued handler).
        return uvicorn_file_log_config(str(uvicorn_log_path), LOG_FORMAT)
    except InstanceAlreadyRunning:
        # Never fall back to console here: continuing would run a second instance
        # that clobbers the live one's log files. Propagate and let main() exit.
        raise
    except Exception as e:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
        logging.getLogger(__name__).warning("File logging setup failed, using console: %s", e)
        return None


def main() -> None:
    args = parse_args()
    try:
        uvicorn_log_config = _setup_logging(args.log_dir)
    except InstanceAlreadyRunning as e:
        print(f"refusing to start: {e}", file=sys.stderr)
        raise SystemExit(3) from None

    # Log SIGTERM so we know when the container/orchestrator kills us.
    # SIGKILL (OOM) can't be caught — but SIGTERM (graceful stop) now logs.
    def _handle_sigterm(signum, frame):
        logger.critical("SIGTERM received — shutting down (pid=%d)", os.getpid())

    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        dump = install_stack_dump(stack_dump_name(args.log_dir, "dlserver"))
        logger.info("Stack dump on SIGUSR1 → %s", dump)
    except OSError as e:
        logger.warning("Stack dump not installed: %s", e)

    if args.pid_file:
        try:
            Path(args.pid_file).write_text(str(os.getpid()))
        except Exception as e:
            logger.warning("Failed to write PID file %s: %s", args.pid_file, e)

    logger.info("Starting DL backend on %s:%d (pid=%d)", args.host, args.port, os.getpid())
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_config=uvicorn_log_config,
        limit_concurrency=200,
    )
