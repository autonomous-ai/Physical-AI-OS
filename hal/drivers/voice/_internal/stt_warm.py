"""Keep the STT socket open while someone is around, not around the clock.

A cold STT connection at speech start costs the first partial 1.5–2.5 s and
loses short utterances entirely ("Stop", "Hey"). An always-open socket costs
money on every idle lamp. The middle: pre-connect while presence says someone
is here or the user spoke recently, and let it go when the room empties.
"""

import logging
import time

logger = logging.getLogger("hal.voice")


def normalize_mode(value: str) -> str:
    """HAL_STT_KEEPALIVE: off | always | presence (legacy true/false map to always/off)."""
    v = (value or "").strip().lower()
    if v in ("1", "true", "yes", "always", "on"):
        return "always"
    if v == "presence":
        return "presence"
    return "off"


def keepalive_wanted(mode: str, *, now: float, last_speech_ts: float, present: bool,
                     warm_after_s: float) -> bool:
    """Whether the STT socket should be open right now."""
    if mode == "always":
        return True
    if mode != "presence":
        return False
    if present:
        return True
    return last_speech_ts > 0 and (now - last_speech_ts) < warm_after_s


def presence_present() -> bool:
    """Whether the presence loop currently sees someone; False when unknown."""
    try:
        from hal import app_state
        from hal.drivers.sensing.presence_service import PresenceState

        sensing = getattr(app_state, "sensing_service", None)
        service = getattr(sensing, "_presense_service", None)
        return getattr(service, "state", None) == PresenceState.PRESENT
    except Exception:
        return False


def now() -> float:
    return time.time()
