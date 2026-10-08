"""Pre-connect a parked realtime session when someone is about to talk.

Speech start already resumes a parked session, but a short question is over
before the reconnect lands. Presence and gaze say "someone is here" seconds
earlier, so the session can be warm by the time the first word arrives.
"""

import logging
import threading
import time

logger = logging.getLogger("hal.voice")

# Attempts are cheap (a no-op unless the session is parked) but gaze samples
# arrive many times a second; one attempt every few seconds is plenty.
PREWARM_MIN_INTERVAL_S = 2.0

_lock = threading.Lock()
_last_attempt = 0.0


def prewarm_realtime(reason: str, now: float | None = None) -> bool:
    """Ask the realtime orchestrator to resume a parked session in the background.

    Returns True only when a resume was actually started.
    """
    global _last_attempt
    now = time.monotonic() if now is None else now
    with _lock:
        if now - _last_attempt < PREWARM_MIN_INTERVAL_S:
            return False
        _last_attempt = now
    try:
        import hal.app_state as state

        voice = getattr(state, "voice_service", None)
        realtime = getattr(voice, "realtime", None) if voice is not None else None
        if realtime is None or not hasattr(realtime, "prewarm"):
            return False
        started = bool(realtime.prewarm())
    except Exception as e:
        logger.debug("[realtime] prewarm on %s skipped: %s", reason, e)
        return False
    if started:
        logger.info("[realtime] prewarm started on %s", reason)
    return started


def reset_for_test() -> None:
    global _last_attempt
    with _lock:
        _last_attempt = 0.0
