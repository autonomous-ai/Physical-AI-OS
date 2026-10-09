"""MPR121 voice-input mode action and bounded, off-poll-thread feedback."""

import logging
import threading

import requests

import hal.app_state as state
from hal import config, privacy
from hal.drivers.button_actions import HoldLEDFeedback

logger = logging.getLogger(__name__)
TOGGLE_URL = "http://127.0.0.1:5000/api/device/voice-input-mode/toggle"


def action_allowed(snapshot):
    return (snapshot is not None and snapshot.get("enabled") is False
            and not snapshot.get("unavailable", False)
            and not state._sleeping and not state._enrolling
            and not privacy.mic_locked())


def request_toggle():
    """Send exactly once: a timeout may mean the OS already applied the toggle."""
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(TOGGLE_URL, timeout=30, allow_redirects=False)
    payload = response.json()
    if (response.status_code != 200 or not isinstance(payload, dict)
            or payload.get("status") != 1 or not isinstance(payload.get("data"), dict)
            or payload["data"].get("mode") not in ("automatic", "tap_to_talk")):
        raise ValueError("Invalid voice-input mode response")
    return payload["data"]["mode"]


def toggle_voice_input_mode(snapshot_provider, *, cancelled=lambda: False):
    """Only acknowledge the applied OS response, never speculative local state."""
    if cancelled() or not action_allowed(snapshot_provider()):
        return
    try:
        mode = request_toggle()
    except (requests.RequestException, ValueError):
        logger.warning("MPR121 voice-input mode toggle failed", exc_info=True)
        return
    # A newer HTTP/MQTT update may have superseded this response already.
    # Privacy/sleep/Harness may also have changed while the request was pending.
    if (cancelled() or not action_allowed(snapshot_provider())
            or getattr(config, "VOICE_INPUT_MODE", "automatic") != mode):
        return
    from hal.drivers.button_actions import _speak_gesture_ack, play_ack_chime
    from hal.i18n import PHRASE_VOICE_AUTOMATIC, PHRASE_VOICE_TAP_TO_TALK, localized_phrase
    play_ack_chime("MPR121")
    _speak_gesture_ack(localized_phrase(
        PHRASE_VOICE_TAP_TO_TALK if mode == "tap_to_talk" else PHRASE_VOICE_AUTOMATIC,
    ), "MPR121")


class ModeToggleWorker:
    """At most one toggle in flight; slow OS requests never queue physical taps."""

    def __init__(self, snapshot_provider, stop):
        self._snapshot_provider = snapshot_provider
        self._stop = stop
        self._busy = threading.Lock()
        self._thread = None

    def submit(self):
        if self._stop.is_set() or not self._busy.acquire(blocking=False):
            return False
        try:
            self._thread = threading.Thread(target=self._run, daemon=True, name="mpr121-mode-toggle")
            self._thread.start()
        except Exception:
            self._busy.release()
            raise
        return True

    def _run(self):
        try:
            toggle_voice_input_mode(self._snapshot_provider, cancelled=self._stop.is_set)
        finally:
            self._busy.release()


class ModeHoldFeedback(HoldLEDFeedback):
    """Use the bounded hold worker protocol with a distinct cyan arming light."""

    def _run(self):
        from hal.drivers.button_actions import dispatch_led
        generation = -1
        showing = False
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or generation != self._generation)
                    if self._closed:
                        return
                    generation, tier = self._generation, self._tier
                try:
                    if tier and self._is_current(generation):
                        showing = True
                        dispatch_led((0, 180, 255), still_current=lambda: self._is_current(generation))
                    elif showing:
                        showing = False
                        state._restore_user_led()
                except Exception:
                    logger.warning("MPR121 mode arming LED failed", exc_info=True)
                finally:
                    with self._condition:
                        self._completed = max(self._completed, generation)
                        self._condition.notify_all()
        finally:
            if showing:
                try:
                    state._restore_user_led()
                except Exception:
                    logger.warning("MPR121 mode arming LED restore failed", exc_info=True)
