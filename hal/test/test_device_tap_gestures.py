"""Device manual recording gestures without GPIO, network, or audio hardware."""

from types import SimpleNamespace
from unittest import mock

import pytest

import hal.app_state as state
from hal import config
from hal.board.mpr121 import MPR121Config
from hal.drivers import button_actions, device_tap_actions, gpio_button
from hal.drivers.mpr121 import MPR121Handler
from hal.drivers.voice._internal import harness_voice


@pytest.fixture
def tap(monkeypatch):
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "tap_to_talk", raising=False)
    voice = SimpleNamespace(device_input=mock.Mock(
        spec=["active", "enabled", "start", "finish", "cancel"], active=False, enabled=True,
    ))
    monkeypatch.setattr(state, "voice_service", voice)
    monkeypatch.setattr(state, "tts_service", SimpleNamespace(speaking=False))
    for name in ("_sleeping", "_mic_muted", "_speaker_muted", "_enrolling", "_hw_mic_switch_muted"):
        monkeypatch.setattr(state, name, False)
    for name in ("_stop_active_tracking", "_cancel_agent_speech", "_wake_if_sleepy",
                 "_grant_wakeword_focus", "announce_listening_cue"):
        monkeypatch.setattr(button_actions, name, mock.Mock())
    from hal.routes import music, voice as routes
    for module, names in ((music, ("audio_stop", "unmute_speaker")),
                          (routes, ("stop_tts", "unmute_mic"))):
        for name in names:
            monkeypatch.setattr(module, name, mock.Mock())
    monkeypatch.setattr(harness_voice, "read_voice_mode", mock.Mock(return_value={"enabled": False, "generation": 1}))
    return voice, routes


def test_taps_start_then_finish_without_spoken_cue_or_focus(tap):
    voice, _ = tap
    device_tap_actions.physical_short_tap()
    voice.device_input.start.assert_called_once_with()
    voice.device_input.active = True
    device_tap_actions.physical_short_tap()
    voice.device_input.finish.assert_called_once_with()
    button_actions.announce_listening_cue.assert_not_called()
    button_actions._grant_wakeword_focus.assert_not_called()


def test_speaking_tap_only_stops_then_next_records(tap):
    voice, routes = tap
    state.tts_service.speaking = True
    device_tap_actions.physical_short_tap()
    voice.device_input.cancel.assert_called_once_with()
    routes.stop_tts.assert_called_once_with()
    voice.device_input.start.assert_not_called()
    state.tts_service.speaking = False
    device_tap_actions.physical_short_tap()
    voice.device_input.start.assert_called_once_with()


@pytest.mark.parametrize("blocked", ["_hw_mic_switch_muted", "_enrolling", "_sleeping"])
def test_privacy_enrollment_and_sleep_do_not_start_capture(tap, monkeypatch, blocked):
    voice, routes = tap
    monkeypatch.setattr(state, blocked, True)
    device_tap_actions.physical_short_tap()
    voice.device_input.start.assert_not_called()
    voice.device_input.finish.assert_not_called()
    routes.unmute_mic.assert_not_called()
    assert button_actions._wake_if_sleepy.call_count == int(blocked == "_sleeping")


def test_software_muted_mic_can_start(tap, monkeypatch):
    voice, routes = tap
    monkeypatch.setattr(state, "_mic_muted", True)
    device_tap_actions.physical_short_tap()
    routes.unmute_mic.assert_called_once_with()
    voice.device_input.start.assert_called_once_with()


def test_unknown_harness_mode_fails_closed(tap):
    voice, _ = tap
    harness_voice.read_voice_mode.return_value = {"enabled": False, "unavailable": True}
    device_tap_actions.physical_short_tap()
    voice.device_input.start.assert_not_called()


def test_automatic_action_is_unchanged(tap, monkeypatch):
    monkeypatch.setattr(config, "VOICE_INPUT_MODE", "automatic")
    with mock.patch.object(button_actions, "single_click_action") as normal:
        device_tap_actions.physical_short_tap("test")
    normal.assert_called_once_with(source="test", announce=False)
    harness_voice.read_voice_mode.assert_not_called()


def test_mpr121_rapid_taps_stay_queued_individually(tap):
    handler = MPR121Handler(MPR121Config(bus=1, debounce_ms=0))
    snapshot = {"enabled": False, "generation": 1}
    handler._harness_gestures = SimpleNamespace(snapshot=snapshot, mode_key=lambda: (False, 1))
    handler._mode_key = (False, 1)
    handler._detector = handler._new_detector()
    for touched, now in [(False, 0), (True, 1), (False, 1.1), (True, 1.2), (False, 1.3), (False, 2)]:
        handler._process_touch(touched, now)
    events = [handler._pending.get_nowait()[1] for _ in range(handler._pending.qsize())]
    assert [event.kind for event in events] == ["single", "single"]
    assert events[0].gesture_id != events[1].gesture_id
    handler._invalidate_pending("harness_mode_changed")
    assert handler._single_generation == handler._generation


def test_gpio_rapid_taps_do_not_arm_triple_click(tap):
    from hal.board.gpio_button import ButtonConfig
    from hal.test.test_gpio_button import ImmediateThread, edge
    with (mock.patch.object(gpio_button, "HoldLEDFeedback"),
          mock.patch.object(gpio_button.threading, "Thread", ImmediateThread),
          mock.patch.object(gpio_button, "physical_short_tap") as action,
          mock.patch.object(gpio_button, "triple_click_action") as triple,
          mock.patch.object(gpio_button, "announce_listening_cue") as cue):
        handler = gpio_button.GPIOButtonHandler(ButtonConfig(chip=0, line=99, debounce_ns=0))
        for start in (1, 1.2, 1.4):
            edge(handler, 0, start)
            edge(handler, 1, start + .1)
        handler._on_click_timeout()
    assert action.call_count == 3
    assert handler._click_timer is None
    triple.assert_not_called()
    cue.assert_not_called()
