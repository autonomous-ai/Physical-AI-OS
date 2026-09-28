"""HAL entrypoint: serve the LED while the full hardware runtime starts."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
import time

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=False)

from fastapi import FastAPI
from fastapi.responses import JSONResponse

import hal.app_state as state
from hal.server_support.boot_config import boot_config
from hal.server_support.early_runtime import EarlyRuntime
from hal.server_support.http_security import (
    ProxyPrefixMiddleware, local_only_middleware, request_logging_middleware,
)

logger = logging.getLogger("uvicorn.error")
_started = time.perf_counter()
_boot = boot_config()
state.safety_policy = _boot.safety


@asynccontextmanager
async def _led_lifespan(app):
    if "led" in _boot.profile.declared_routes():
        try:
            from hal.drivers.rgb.rgb_service import RGBService

            service = RGBService(led_count=_boot.led_count, safety_policy=_boot.safety)
            service.start()
            state.rgb_service = service
            logger.info("[startup] led_ready elapsed_ms=%.0f", (time.perf_counter() - _started) * 1000)
        except Exception:
            # Full startup retains the existing driver validation/retry path.
            logger.exception("Early LED initialization failed")
    yield
    # Ownership passes to the full lifespan; the dispatcher cleans up here
    # only when full startup never entered successfully.


_bootstrap = FastAPI(lifespan=_led_lifespan, docs_url=None, redoc_url=None, openapi_url=None)
if "led" in _boot.profile.declared_routes():
    from hal.routes.led import router as _led_router

    _bootstrap.include_router(_led_router)


@_bootstrap.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def _starting(path: str):
    # In particular /health must not claim that voice/camera/servo are ready.
    return JSONResponse({"detail": "HAL is starting"}, status_code=503, headers={"Retry-After": "1"})


_bootstrap.add_middleware(ProxyPrefixMiddleware)
_bootstrap.middleware("http")(local_only_middleware)
_bootstrap.middleware("http")(request_logging_middleware)


def _load_runtime():
    from hal.runtime import app

    return app


def _cleanup_early():
    state._stop_current_effect()
    if state.rgb_service is not None:
        state.rgb_service.stop()
        state.rgb_service = None


app = EarlyRuntime(_bootstrap, _load_runtime, _cleanup_early)

if __name__ == "__main__":
    import uvicorn
    from hal.config import HTTP_HOST, HTTP_PORT

    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT)
