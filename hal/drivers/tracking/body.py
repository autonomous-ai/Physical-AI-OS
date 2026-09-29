"""Handing the body back to idle after a direct move parked it."""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

HOLD_AFTER_FIND_S: float = 8.0

_pending: Optional[threading.Timer] = None
_pending_lock = threading.Lock()


def release_to_idle(reason: str) -> bool:
    """Dispatch play(idle) now, unless someone owns the body or something is already
    playing. Never raises.
    """
    import hal.app_state as state

    svc = getattr(state, "animation_service", None)
    if svc is None:
        return False
    if getattr(svc, "_tracking_active", False):
        return False
    if getattr(svc, "_hold_mode", False) or getattr(svc, "_zero_mode", False):
        return False
    if getattr(svc, "_current_recording", None) is not None:
        return False
    try:
        # Dispatch rather than _handle_play: playback belongs to the event
        # thread, the same handover the tracker performs when it ends.
        svc.dispatch("play", svc.idle_recording)
        logger.info("[body] %s — idle resumed", reason)
        return True
    except Exception as e:
        logger.warning("[body] could not resume idle after %s: %s", reason, e)
        return False


def cancel_pending_release() -> None:
    """Drop a scheduled handback, if any."""
    global _pending
    with _pending_lock:
        if _pending is not None:
            _pending.cancel()
            _pending = None


def release_to_idle_later(delay_s: float, reason: str) -> Optional[threading.Timer]:
    """Hand the body back after `delay_s`, replacing any earlier schedule."""
    global _pending
    cancel_pending_release()
    if delay_s <= 0:
        release_to_idle(reason)
        return None

    def _fire() -> None:
        global _pending
        with _pending_lock:
            _pending = None
        release_to_idle(reason)

    t = threading.Timer(delay_s, _fire)
    t.daemon = True
    t.name = "body-release-to-idle"
    with _pending_lock:
        _pending = t
    t.start()
    return t
