"""DL Backend Load Balancer: round-robin reverse proxy with optional RSA/AES encryption.

Requests are forwarded to backends under INTERNAL_PREFIX. With crypto enabled,
HTTP bodies carry a per-request RSA-wrapped AES key; WS does one key exchange
up front and encrypts every frame in both directions.
"""

import argparse
import asyncio
import logging
import logging.handlers
import os
import re
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
import websockets
from cryptography.exceptions import InvalidTag
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from config import settings
from core.crypto.rsa_aes import AESGCMSession, RSAAESCrypto
from core.models.crypto import AESGCMPlainPayload
from lbserver.models import WSCipherMessage, WSKeyExchangeRequest
from lbserver.routes.crypto import router as crypto_router
from lbserver.utils import RoundRobin
from lbserver.utils.crypto import encrypt_http_response, try_decrypt_http_body
from lbserver.utils.switch import read_active, resolve_backends, write_ack
from core.livez import router as livez_router
from core.logging_ext import ResilientRotatingFileHandler, queued, uvicorn_file_log_config
from core.stackdump import install_stack_dump, stack_dump_name
from core.request_context import (
    InstanceAlreadyRunning,
    acquire_instance_lock,
    install_request_id_logging,
    request_id_middleware,
)
from lbserver.utils.state import get_crypto, set_crypto

LOG_FORMAT = "%(asctime)s [%(name)s] [%(request_id)s] %(levelname)s: %(message)s"

# A regex, not json.loads: frames are ~100 KB and this runs on the event loop for
# every message, only to build a log line.
_FRAME_B64 = re.compile(r'"frame_b64"\s*:\s*"([^"]*)"')


def _loggable_ws_text(data: str) -> str:
    """Log form of a client WS message: the base64 frame replaced by its length."""
    return _FRAME_B64.sub(
        lambda m: f'"frame_b64": "<{len(m.group(1))} chars>"', data
    )[:100]


# Must run before any record is emitted: LOG_FORMAT references %(request_id)s.
install_request_id_logging()

_instance_lock: object | None = None
logger = logging.getLogger("lbserver")

INTERNAL_PREFIX: str = settings.lb.internal_prefix
BACKENDS: list[str] = [b.strip().rstrip("/") for b in settings.lb.backends.split(",") if b.strip()]

if not BACKENDS:
    logger.warning("No backends configured — set LB__BACKENDS=http://127.0.0.1:8888")


http_rr = RoundRobin(BACKENDS)
ws_rr = RoundRobin(BACKENDS)

STATE_FILE: Path | None = Path(settings.lb.state_file) if settings.lb.state_file else None


def apply_state_file() -> list[str]:
    """Point http_rr/ws_rr at the slot named in lb.state_file and ack it.

    proxy_http/proxy_ws read these module globals on every request, so rebinding
    them moves new requests; in-flight requests and open WebSockets keep the
    backend they already picked.
    """
    global http_rr, ws_rr
    if STATE_FILE is None:
        return list(BACKENDS)
    backends = resolve_backends(BACKENDS, read_active(STATE_FILE))
    http_rr, ws_rr = RoundRobin(backends), RoundRobin(backends)
    write_ack(STATE_FILE, backends)
    logger.warning("[switch] backends -> %s", ", ".join(backends))
    return backends


def install_switch() -> None:
    """Apply lb.state_file now and again on every SIGHUP.

    A bad state file never takes lbserver down: the error is logged, the current
    backends stay and no ack is written, so the deploy script sees the switch did
    not happen and rolls back.
    """
    if STATE_FILE is None:
        return

    def _apply(context: str) -> None:
        try:
            apply_state_file()
        except Exception:
            logger.exception(
                "[switch] %s: state file not applied, keeping backends", context
            )

    _apply("startup")
    signal.signal(signal.SIGHUP, lambda signum, frame: _apply("SIGHUP"))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if settings.crypto.enabled:
        crypto = RSAAESCrypto(
            key_dir=settings.crypto.key_dir,
            key_size=settings.crypto.key_size,
        )
        set_crypto(crypto)
        logger.info("Encryption enabled (key_dir=%s)", settings.crypto.key_dir)

    # Created last so an earlier startup failure cannot leak it.
    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.lb.http_timeout, connect=settings.lb.connect_timeout
        ),
        limits=httpx.Limits(
            max_connections=settings.lb.max_connections,
            max_keepalive_connections=settings.lb.max_keepalive,
        ),
    )
    logger.info(
        "HTTP client pool ready (max_connections=%s keepalive=%s timeout=%ss connect=%ss)",
        settings.lb.max_connections,
        settings.lb.max_keepalive,
        settings.lb.http_timeout,
        settings.lb.connect_timeout,
    )
    try:
        yield
    finally:
        await app.state.http_client.aclose()
        app.state.http_client = None
        set_crypto(None)


app = FastAPI(title="DL Backend Load Balancer", lifespan=_lifespan)
app.middleware("http")(request_id_middleware)
app.include_router(crypto_router, prefix="/api/crypto")
# Must be registered before the catch-all proxy route, or /livez gets forwarded.
app.include_router(livez_router)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_http(request: Request, path: str) -> Response:
    backend: str = http_rr.next()
    url: str = f"{backend}{INTERNAL_PREFIX}/{path}"

    headers: dict[str, str] = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "transfer-encoding", "content-length")
    }

    body: bytes = await request.body()
    encrypted_key: bytes | None = None

    # Each encrypted request carries its own RSA-wrapped AES key; the response is
    # encrypted with the same key. encrypted_key is None means plaintext exchange.
    if request.method in ("POST", "PUT", "PATCH") and body:
        body, encrypted_key = try_decrypt_http_body(body)

    client: httpx.AsyncClient | None = getattr(request.app.state, "http_client", None)
    if client is None:  # pragma: no cover - only if lifespan did not run
        logger.error("[HTTP] No pooled client; lifespan did not run")
        raise HTTPException(status_code=503, detail="Proxy not ready")

    try:
        resp = await client.request(
            method=request.method,
            url=url,
            headers=headers,
            params=dict(request.query_params),
            content=body,
        )
    # TimeoutException subclasses RequestError, so it must be caught first.
    except httpx.TimeoutException:
        logger.error(
            "[HTTP] Backend timed out after %ss: %s", settings.lb.http_timeout, backend
        )
        raise HTTPException(status_code=504, detail=f"Backend timed out: {backend}")
    except httpx.RequestError as e:
        logger.error("[HTTP] Backend unreachable: %s -- %s", backend, e)
        raise HTTPException(status_code=502, detail=f"Backend unreachable: {backend}")

    content = resp.content
    resp_headers = dict(resp.headers)

    logger.info(
        "[HTTP] %s /%s → %s (encrypted=%s): %s %s",
        request.method,
        path,
        url,
        encrypted_key is not None,
        resp.status_code,
        resp.text[:100],
    )

    if encrypted_key is not None:
        content = encrypt_http_response(content, encrypted_key)
        resp_headers["content-type"] = "application/json"
        resp_headers.pop("content-length", None)

    return Response(
        content=content,
        status_code=resp.status_code,
        headers=resp_headers,
    )


@app.websocket("/{path:path}")
async def proxy_ws(client_ws: WebSocket, path: str) -> None:
    backend: str = ws_rr.next()
    ws_backend: str = backend.replace("http://", "ws://").replace("https://", "wss://")
    ws_url: str = f"{ws_backend}{INTERNAL_PREFIX}/{path}"

    extra_headers: dict[str, str] = {}
    for key in ("x-api-key", "authorization"):
        val: str | None = client_ws.headers.get(key)
        if val:
            extra_headers[key] = val

    await client_ws.accept()
    logger.info("[WS] /%s → %s", path, ws_url)

    crypto = get_crypto()
    session: AESGCMSession | None = None

    # Key exchange happens before connecting to the backend. Close 1008 = missing or
    # invalid handshake while require_encryption; 1011 = AES key unwrap failed.
    # Without require_encryption a non-handshake first frame is forwarded as plaintext.
    first_msg: str | None = None
    if crypto is not None:
        try:
            first_msg = await asyncio.wait_for(client_ws.receive_text(), timeout=5.0)
            try:
                key_req = WSKeyExchangeRequest.model_validate_json(first_msg)
                session = crypto.create_session(key_req.to_raw_key())
                await client_ws.send_json({"status": "key_exchange_ok"})
                logger.info("[WS] /%s → %s: Encrypted session established", path, ws_url)
                first_msg = None
            except ValidationError:
                if settings.crypto.require_encryption:
                    await client_ws.close(code=1008, reason="Key exchange required")
                    return
        except asyncio.TimeoutError:
            if settings.crypto.require_encryption:
                await client_ws.close(code=1008, reason="Key exchange required")
                return
            first_msg = None
        except (ValueError, InvalidTag) as e:
            logger.error("[WS] /%s → %s: Key exchange failed: %s", path, ws_url, e)
            await client_ws.close(code=1011, reason=f"Key exchange failed: {e}")
            return

    try:
        async with websockets.connect(ws_url, additional_headers=extra_headers, open_timeout=settings.lb.ws_open_timeout) as backend_ws:
            if first_msg is not None:
                await backend_ws.send(first_msg)

            async def client_to_backend() -> None:
                try:
                    while True:
                        data: str = await client_ws.receive_text()

                        if session is not None:
                            try:
                                enc_msg = WSCipherMessage.model_validate_json(data)
                                result = session.decrypt(enc_msg.to_raw_payload())
                                data = result.plain_data.decode()
                            except ValidationError:
                                logger.warning(
                                    "[WS] /%s → %s: Rejecting unencrypted message",
                                    path,
                                    ws_url,
                                )
                                continue
                            except (InvalidTag, ValueError) as e:
                                logger.error("[WS] /%s → %s: Decrypt failed: %s", path, ws_url, e)
                                continue
                        logger.info(
                            "[WS] /%s → %s (encrypted=%s): %s",
                            path,
                            ws_url,
                            session is not None,
                            _loggable_ws_text(data),
                        )
                        await backend_ws.send(data)
                except WebSocketDisconnect:
                    await backend_ws.close()

            async def backend_to_client() -> None:
                try:
                    async for msg in backend_ws:
                        if isinstance(msg, str):
                            logger.info(
                                "[WS] %s → /%s (encrypted=%s): %s",
                                ws_url,
                                path,
                                session is not None,
                                msg[:100],
                            )
                            if session is not None:
                                encrypted = session.encrypt(
                                    AESGCMPlainPayload(plain_data=msg.encode())
                                )
                                msg = WSCipherMessage.from_raw_payload(encrypted).model_dump_json()
                            await client_ws.send_text(msg)
                        else:
                            logger.info(
                                "[WS] %s → /%s (encrypted=%s): %s",
                                ws_url,
                                path,
                                session is not None,
                                msg[:100].decode(),
                            )
                            if session is not None:
                                encrypted = session.encrypt(AESGCMPlainPayload(plain_data=msg))
                                msg = WSCipherMessage.from_raw_payload(encrypted).model_dump_json()
                                await client_ws.send_text(msg)
                            else:
                                await client_ws.send_bytes(msg)
                except websockets.exceptions.ConnectionClosed:
                    pass

            tasks = [
                asyncio.create_task(client_to_backend()),
                asyncio.create_task(backend_to_client()),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t.exception():
                        logger.warning("[WS] proxy task failed: %s", t.exception())
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    except TimeoutError as e:
        # Raised by websockets on open_timeout (hung backend).
        logger.error("[WS] Backend handshake timed out: %s — %s", backend, e)
        await client_ws.close(code=1011, reason=f"Backend timed out: {backend}")
    except (websockets.exceptions.InvalidStatus, OSError) as e:
        logger.error("[WS] Backend connection failed: %s — %s", backend, e)
        await client_ws.close(code=1011, reason=f"Backend unreachable: {backend}")
    except WebSocketDisconnect:
        logger.info("[WS] Client disconnected: /%s", path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DL Backend Load Balancer")
    parser.add_argument("--host", default=settings.lb.host)
    parser.add_argument("--port", type=int, default=settings.lb.port)
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
        # Must be taken before the rotation below, which renames/unlinks unconditionally.
        global _instance_lock
        _instance_lock = acquire_instance_lock(log_dir)
        log_path = Path(log_dir) / "lbserver.log"
        uvicorn_log_path = Path(log_dir) / "uvicorn.log"
        for prefix in ("lbserver.log", "uvicorn.log"):
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
        # Never fall back to console: a second instance would clobber the live one's logs.
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

    def _handle_sigterm(signum, frame):
        logger.critical("SIGTERM received — shutting down (pid=%d)", os.getpid())

    signal.signal(signal.SIGTERM, _handle_sigterm)
    install_switch()
    try:
        dump = install_stack_dump(stack_dump_name(args.log_dir, "lbserver"))
        logger.info("Stack dump on SIGUSR1 → %s", dump)
    except OSError as e:
        logger.warning("Stack dump not installed: %s", e)

    if args.pid_file:
        try:
            Path(args.pid_file).write_text(str(os.getpid()))
        except Exception as e:
            logger.warning("Failed to write PID file %s: %s", args.pid_file, e)

    if BACKENDS:
        logger.info("Backends: %s", ", ".join(BACKENDS))
    else:
        logger.error("No backends — set LB__BACKENDS in .env")
    logger.info("Internal prefix: %s", INTERNAL_PREFIX)
    logger.info("Starting load balancer on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_config=uvicorn_log_config)
