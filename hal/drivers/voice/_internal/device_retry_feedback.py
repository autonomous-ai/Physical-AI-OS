"""Optional recovery speech for explicit device captures no provider understood."""

import logging

from hal.drivers.voice._internal.realtime_turn import (
    ROUTE_ERROR, ROUTE_NOT_STARTED, ROUTE_NO_OUTPUT, ROUTE_UNAVAILABLE,
)
from hal.i18n import PHRASE_VOICE_RETRY, localized_phrase

logger = logging.getLogger("hal.voice")
_RETRY_ROUTES = {ROUTE_ERROR, ROUTE_NOT_STARTED, ROUTE_NO_OUTPUT, ROUTE_UNAVAILABLE}


def maybe_emit_retry(tts, capture, combined, speech_confirmed, realtime_result, valid):
    """Admit one optional cue after both providers settle without usable input.

    The caller must distinguish genuinely empty STT from text removed by an
    echo or fabrication filter. ``speech_confirmed`` must come from a successful
    speech detector, never its fail-open default. ``valid`` takes no arguments.
    """
    snapshot = capture.snapshot
    stamp = snapshot.get("capturedAtMs")
    if (tts is None or combined.strip() or speech_confirmed is not True
            or snapshot.get("enabled") is not False
            or snapshot.get("deviceInputMode") != "tap_to_talk"
            or snapshot.get("unavailable", False)
            or type(stamp) is not int or not 10**12 <= stamp < 10**13
            or realtime_result.route not in _RETRY_ROUTES
            or realtime_result.handled or realtime_result.delegated
            or realtime_result.execution_completed or realtime_result.rejected):
        return False

    def active():
        return not capture.cancelled.is_set() and valid()

    if not active():
        return False
    phrase = localized_phrase(PHRASE_VOICE_RETRY)
    if not phrase:
        return False
    try:
        accepted = tts.speak_cached(
            phrase, interruptible=True, realtime_feedback=False,
            turn_id=f"tap-retry-{stamp}", _device_turn_valid=active,
        )
    except Exception:
        logger.exception("Device retry feedback failed")
        return False
    accepted = bool(accepted) and active()
    if accepted:
        logger.info("Device retry feedback admitted: empty STT, confirmed speech, route=%s",
                    realtime_result.route)
    return accepted
