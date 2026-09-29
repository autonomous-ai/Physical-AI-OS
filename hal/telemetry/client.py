"""Device-side pipe for HAL analytics events: local log line, bounded queue, one sender thread, POST to os-server.

:func:`report` never blocks; every event is logged locally before it is sent.
"""

import json
import logging
import os
import queue
import threading
import uuid

import requests

logger = logging.getLogger("hal.telemetry")

OS_TELEMETRY_URL = "http://127.0.0.1:5000/api/telemetry/event"

# Unset endpoint = nothing leaves the device; events are still logged locally.
ENV_ANALYTICS_URL = "AUTONOMOUS_ANALYTICS_URL"


def enabled() -> bool:
    """Whether events may leave the device (read per call)."""
    return bool(os.environ.get(ENV_ANALYTICS_URL, "").strip())

# Bounded so a dead uplink cannot grow memory; drops are counted, not buffered.
QUEUE_SIZE = 128
POST_TIMEOUT_S = 3.0

_queue: "queue.Queue" = queue.Queue(maxsize=QUEUE_SIZE)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()

_dropped = 0
_failed = 0
_counter_lock = threading.Lock()


def new_event_id() -> str:
    """A unique id for one observation, used by os-server to de-duplicate."""
    return uuid.uuid4().hex


def report(event_name: str, params: dict, event_id: str = "") -> None:
    """Queue one telemetry event. Non-blocking; never raises."""
    if not event_name:
        return
    try:
        event_id = event_id or new_event_id()
        payload = {
            "event_name": event_name,
            "event_id": event_id,
            "params": _with_counters(params or {}),
        }
        logger.info(
            "[telemetry] %s %s", event_name,
            json.dumps({**payload["params"], "event_id": event_id}, default=str),
        )
        if not enabled():
            logger.debug("[telemetry] not sent -- no %s configured", ENV_ANALYTICS_URL)
            return
        _ensure_worker()
        try:
            _queue.put_nowait(payload)
        except queue.Full:
            with _counter_lock:
                global _dropped
                _dropped += 1
                dropped = _dropped
            logger.warning(
                "[telemetry] event dropped -- queue full (event=%s id=%s dropped_total=%d)",
                event_name, event_id, dropped,
            )
    except Exception:
        # A tracker must never take the voice path down with it.
        logger.exception("[telemetry] report failed (event=%s)", event_name)


def stats() -> dict:
    """Delivery health for this process: events dropped and POSTs failed."""
    with _counter_lock:
        return {"dropped": _dropped, "failed": _failed}


def _with_counters(params: dict) -> dict:
    with _counter_lock:
        return {**params, "hal_dropped_total": _dropped, "hal_failed_total": _failed}


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_run, name="telemetry-sender", daemon=True)
        _worker.start()


def _run() -> None:
    while True:
        payload = _queue.get()
        try:
            resp = requests.post(OS_TELEMETRY_URL, json=payload, timeout=POST_TIMEOUT_S)
            if resp.status_code != 200:
                _note_failure(payload, f"os-server returned {resp.status_code}")
        except Exception as e:  # noqa: BLE001 - transport errors are expected offline
            _note_failure(payload, str(e))


def _note_failure(payload: dict, reason: str) -> None:
    with _counter_lock:
        global _failed
        _failed += 1
        failed = _failed
    logger.warning(
        "[telemetry] delivery failed (event=%s id=%s failed_total=%d): %s",
        payload.get("event_name"), payload.get("event_id"), failed, reason,
    )
