"""Physical action orchestration; recognition and HTTP remain independent."""

import logging
import uuid

import hal.app_state as state
from hal.drivers.harness.client import HarnessGestureError, request_voice_toggle
from hal.i18n import (PHRASE_HARNESS_FAILED, PHRASE_HARNESS_NO_AGENTS,
                      PHRASE_HARNESS_OFF, PHRASE_HARNESS_OFFLINE,
                      PHRASE_HARNESS_ON, PHRASE_HARNESS_UNPAIRED, localized_phrase)

logger = logging.getLogger(__name__)


def confirmation_phrase(result: dict) -> str:
    if not result["enabled"]:
        return localized_phrase(PHRASE_HARNESS_OFF)
    return localized_phrase(PHRASE_HARNESS_ON).format(agent=result.get("agentName") or result["agentId"])


def failure_phrase(code: str) -> str:
    key = {"harness_unpaired": PHRASE_HARNESS_UNPAIRED,
           "harness_offline": PHRASE_HARNESS_OFFLINE,
           "no_agents": PHRASE_HARNESS_NO_AGENTS}.get(code, PHRASE_HARNESS_FAILED)
    return localized_phrase(key)


def _show_feedback(enabled: bool):
    """Maintain the ON indicator or briefly blink OFF using device presets."""
    from hal.drivers.harness import led as harness_voice_led
    harness_voice_led.set_enabled(enabled)
    if enabled or not state.rgb_service:
        return
    try:
        from hal.models import LEDEffectRequest
        from hal.presets import BUTTON_LED_PRESETS
        from hal.routes.led import start_led_effect
        preset = BUTTON_LED_PRESETS["harness_off"]
        request = LEDEffectRequest(**preset, transient=True)
        previous = state._effect_thread
        start_led_effect(request)
        if state._effect_thread is not previous:
            state._schedule_led_restore(request.duration_ms / 1000 + 0.1)
    except Exception:
        logger.warning("Harness voice LED feedback failed", exc_info=True)


def toggle_harness_voice():
    """Run on the input action worker, never the hardware polling thread."""
    if state._hw_mic_switch_muted is True:
        return
    if state._sleeping:
        logger.info("MPR121 Harness swipe ignored -- wake the device first")
        return
    from hal.drivers.button_actions import _speak_gesture_ack
    try:
        result = request_voice_toggle(str(uuid.uuid4()))
    except HarnessGestureError as exc:
        logger.warning("Harness voice gesture failed: %s", exc.code)
        _speak_gesture_ack(failure_phrase(exc.code), "MPR121")
        return
    _show_feedback(result["enabled"])
    _speak_gesture_ack(confirmation_phrase(result), "MPR121")
