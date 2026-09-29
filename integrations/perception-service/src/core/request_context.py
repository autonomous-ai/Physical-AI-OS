"""Request-ID propagation across nginx -> lbserver -> dlserver.

`install_request_id_logging()` must run before any record is emitted; a LogRecord
factory guarantees every record (incl. third-party) has %(request_id)s.
"""

from __future__ import annotations

import contextvars
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import Request, Response

REQUEST_ID_HEADER = "x-request-id"
NO_REQUEST_ID = "-"

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=NO_REQUEST_ID
)

_installed = False


def install_request_id_logging() -> None:
    """Make every LogRecord carry `request_id`. Idempotent."""
    global _installed
    if _installed:
        return
    old_factory = logging.getLogRecordFactory()

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = old_factory(*args, **kwargs)
        record.request_id = request_id_var.get()
        return record

    logging.setLogRecordFactory(factory)
    _installed = True


def new_request_id() -> str:
    return uuid.uuid4().hex


async def request_id_middleware(
    request: "Request", call_next: Callable[["Request"], Awaitable["Response"]]
) -> "Response":
    """Bind the inbound X-Request-ID (or mint one) for the duration of the request."""
    rid = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
    token = request_id_var.set(rid)
    try:
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = rid
        return response
    finally:
        request_id_var.reset(token)


class InstanceAlreadyRunning(RuntimeError):
    """Another process already holds the log-directory lock."""


def acquire_instance_lock(log_dir: str) -> "object":
    """Take an exclusive, non-blocking flock on <log_dir>/.instance.lock.

    Must be called before startup log rotation. Returns the open file; keep a
    reference for the process lifetime, since closing it releases the lock.
    """
    import fcntl
    from pathlib import Path

    Path(log_dir).mkdir(parents=True, exist_ok=True)
    lock_path = Path(log_dir) / ".instance.lock"
    handle = lock_path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise InstanceAlreadyRunning(
            f"another instance already holds {lock_path}; refusing to start "
            f"(starting a second instance would clobber the running one's logs)"
        ) from exc
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle
