"""HAL HTTP access control: loopback / same-origin / bearer-token gate."""

import logging
import secrets
import time
from ipaddress import ip_address, ip_network

from fastapi.responses import JSONResponse

from hal.config import DEVICE_AUTH_TOKEN, MODE

logger = logging.getLogger("hal.server")


class ProxyPrefixMiddleware:
    """ASGI middleware: reads X-Forwarded-Prefix and sets root_path."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            prefix = headers.get(b"x-forwarded-prefix", b"").decode()
            if prefix:
                scope["root_path"] = prefix
        await self.app(scope, receive, send)


_LOCAL_NETS = (
    ip_network("127.0.0.0/8"),
    ip_network("::1/128"),
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
)


def _is_local(value: str | None) -> bool:
    if not value:
        return False
    host = value.split(",")[0].strip()
    if host.startswith("[") and "]" in host:
        host = host[1: host.index("]")]
    elif ":" in host and host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    try:
        addr = ip_address(host)
    except ValueError:
        return host == "localhost"
    return any(addr in net for net in _LOCAL_NETS)


def _is_same_origin(origin_or_referer: str | None, host: str) -> bool:
    if not origin_or_referer:
        return False
    value = origin_or_referer.split(",")[0].strip()
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = value.split("/")[0]
    return value == host


def _has_valid_bearer_token(request) -> bool:
    """True if the request carries Authorization: Bearer <DEVICE_AUTH_TOKEN> (empty token disables)."""
    if not DEVICE_AUTH_TOKEN:
        return False
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return False
    provided = auth[len("Bearer "):].strip()
    if not provided:
        return False
    return secrets.compare_digest(provided, DEVICE_AUTH_TOKEN)


async def local_only_middleware(request, call_next):
    if MODE == "production":
        client = request.client.host if request.client else None
        xff = request.headers.get("x-forwarded-for")
        real_ip = request.headers.get("x-real-ip")

        if _is_local(client) and not (xff and not _is_local(xff)) and not (real_ip and not _is_local(real_ip)):
            return await call_next(request)

        if _has_valid_bearer_token(request):
            return await call_next(request)

        # /docs and /openapi.json are iframe-only; direct navigation has no Referer and is blocked.
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        if _is_same_origin(origin, host) or _is_same_origin(referer, host):
            return await call_next(request)

        logger.warning(
            "Blocked external request: client=%s xff=%s origin=%s referer=%s path=%s",
            client, xff, origin, referer, request.url.path,
        )
        return JSONResponse(
            status_code=403,
            content={"detail": "HAL API: requires loopback, valid bearer token, or same-origin"},
        )
    return await call_next(request)


async def request_logging_middleware(request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.debug(
        "%s %s -> %d (%.1fms)",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response
