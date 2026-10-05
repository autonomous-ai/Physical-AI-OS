"""Harness-only touch actions; the normal physical-control actions stay separate."""

import logging
import threading
import uuid

import hal.app_state as state
from hal.drivers.harness.actions import confirmation_phrase, failure_phrase, _show_feedback
from hal.drivers.harness.client import HarnessGestureError, request_voice_disable, request_focus_step

logger = logging.getLogger(__name__)
EXIT_HOLD_SECONDS = 2.0


def harness_button_recognizer(debounce_ms):
    from hal.drivers.mpr121 import _GestureRecognizer
    return _GestureRecognizer(debounce_ms, hold_thresholds=(EXIT_HOLD_SECONDS,), multi_click=False, hold_on_threshold=True)


def read_mode():
    from hal.drivers.voice._internal.harness_voice import read_voice_mode
    return read_voice_mode()


def routing_key(snapshot):
    return tuple(snapshot.get(key) for key in
                 ("enabled", "generation", "unavailable", "focusAvailable", "machineId", "agentId", "focusRevision"))


class HarnessGestures:
    """Poll OS away from I2C and keep gesture dispatch bound to its original mode."""

    def __init__(self):
        self.snapshot = {"enabled": False, "unavailable": True}
        self._stop = threading.Event()
        self._thread = None

    def mode_key(self):
        return routing_key(self.snapshot)

    def _update(self, snapshot):
        if routing_key(snapshot) != self.mode_key():
            self.cancel_capture()
        self.snapshot = snapshot
        from hal.drivers.harness import led as harness_voice_led
        harness_voice_led.set_enabled(snapshot.get("enabled", False))

    def start(self):
        self._update(read_mode())
        self._thread = threading.Thread(target=self._watch, daemon=True, name="mpr121-harness-mode")
        self._thread.start()

    def _watch(self):
        while not self._stop.wait(0.25):
            self._update(read_mode())

    def stop(self):
        self._stop.set()
        from hal.drivers.harness import led as harness_voice_led
        harness_voice_led.set_enabled(False)
        self.cancel_capture()
        if self._thread:
            self._thread.join(timeout=1)

    def refresh_matches(self, expected):
        snapshot = read_mode()
        self._update(snapshot)
        return not snapshot.get("unavailable") and routing_key(snapshot) == expected

    @staticmethod
    def cancel_capture():
        if state.voice_service:
            state.voice_service.cancel_harness_capture()

    def execute(self, event):
        if event.kind == "single":
            self._tap()
        elif event.kind == "hold" and event.held_s >= EXIT_HOLD_SECONDS:
            self._disable()
        elif event.kind == "swipe":
            self.cancel_capture()
            self._focus(event.direction)

    def _tap(self):
        from hal.drivers.button_actions import _cancel_agent_speech, _wake_if_sleepy, play_ack_chime
        from hal.routes.voice import stop_tts
        if state._sleeping:
            if state._hw_mic_switch_muted is True:
                return
            # Enabling Harness does not wake the body. Consume this tap only
            # for wake; recording requires a separate, subsequent tap.
            self.cancel_capture()
            logger.info("MPR121 Harness tap -- waking only; tap again to record")
            _wake_if_sleepy("MPR121 Harness")
            return
        voice = state.voice_service
        if state.tts_service and state.tts_service.speaking:
            self.cancel_capture()
            _cancel_agent_speech("MPR121 Harness")
            stop_tts()
            play_ack_chime("MPR121 Harness")
            return
        if voice is None or state._hw_mic_switch_muted is True:
            return
        if voice.harness_capture_active:
            voice.finish_harness_capture()
        else:
            voice.start_harness_capture(dict(self.snapshot))

    def _disable(self):
        from hal.drivers.button_actions import _speak_gesture_ack
        self.cancel_capture()
        try:
            result = request_voice_disable(str(uuid.uuid4()))
        except HarnessGestureError as exc:
            _speak_gesture_ack(failure_phrase(exc.code), "MPR121")
            return
        self._update(result)
        _show_feedback(False)
        _speak_gesture_ack(confirmation_phrase(result), "MPR121")

    def _focus(self, direction):
        from hal.drivers.button_actions import _speak_gesture_ack
        from hal.i18n import PHRASE_HARNESS_FOCUS, PHRASE_HARNESS_FOCUS_FAILED, localized_phrase
        if direction not in (-1, 1):
            return
        try:
            result = request_focus_step(str(uuid.uuid4()),
                                        "next" if direction == -1 else "previous",
                                        self.snapshot["generation"])
        except HarnessGestureError as exc:
            logger.warning("Harness focus gesture failed: %s", exc.code)
            phrase = (failure_phrase(exc.code) if exc.code in ("harness_offline", "harness_unpaired", "no_agents")
                      else localized_phrase(PHRASE_HARNESS_FOCUS_FAILED))
            _speak_gesture_ack(phrase, "MPR121")
            return
        self._update(result)
        _speak_gesture_ack(localized_phrase(PHRASE_HARNESS_FOCUS), "MPR121")
