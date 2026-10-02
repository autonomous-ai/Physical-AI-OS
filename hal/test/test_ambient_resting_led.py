"""Resting LED uses device presets without undoing an explicit off command."""
from unittest.mock import Mock

import pytest

from hal import app_state as state, presets
from hal.drivers.harness import led as harness_led
from hal.models import LEDOffRequest
from hal.routes import led


@pytest.fixture
def lamp(monkeypatch, tmp_path):
    monkeypatch.setitem(presets.AMBIENT_RESTING_LED, "effect", "solid")
    monkeypatch.setitem(presets.AMBIENT_RESTING_LED, "color", [5, 4, 3])
    monkeypatch.setattr(state, "rgb_service", Mock())
    monkeypatch.setattr(state, "sensing_service", None)
    monkeypatch.setattr(state, "_user_led_state", None)
    monkeypatch.setattr(state, "_LED_STATE_PATH", str(tmp_path / "led.json"))
    for flag in ("_sleeping", "_tts_speaking", "_music_playing", "_thinking_cue_active"):
        monkeypatch.setattr(state, flag, False)
    monkeypatch.setattr(state, "_restore_timer", None)
    monkeypatch.setattr(state, "_stop_current_effect", Mock())
    monkeypatch.setattr(state, "_mic_muted_led_owns_strip", lambda: False)
    monkeypatch.setattr(state, "_dismiss_mic_muted_led", Mock())
    monkeypatch.setattr(harness_led, "restore", lambda: False)
    monkeypatch.setattr(led, "_end_scene", Mock())
    # A solid preset must never create an effect worker.
    monkeypatch.setattr(state.threading, "Thread", Mock(side_effect=AssertionError("solid started a worker")))
    return state.rgb_service


def test_default_restores_solid_without_effect_thread(lamp):
    led.restore_led()
    lamp.dispatch.assert_called_once_with("solid", (5, 4, 3))
    assert state._user_led_state is None
    assert not state.led_should_stay_dark()


def test_off_survives_restore_and_sidecar_reload(lamp):
    led.turn_off_leds(LEDOffRequest())
    assert state._load_user_led_state() == {"type": "solid", "color": [0, 0, 0]}
    assert state.led_should_stay_dark()
    led.restore_led()
    lamp.dispatch.assert_called_once_with("solid", (0, 0, 0))


def test_transient_off_returns_to_device_default(lamp):
    led.turn_off_leds(LEDOffRequest(transient=True))
    assert state._user_led_state is None
    led.restore_led()
    lamp.dispatch.assert_called_once_with("solid", (5, 4, 3))


@pytest.mark.parametrize("flag", ["_tts_speaking", "_music_playing"])
def test_ambient_restore_does_not_interrupt_playback(lamp, monkeypatch, flag):
    monkeypatch.setattr(state, flag, True)
    led.restore_led()
    lamp.dispatch.assert_not_called()


def test_explicit_color_survives_ambient_restore(lamp, monkeypatch):
    monkeypatch.setattr(state, "_user_led_state", {"type": "solid", "color": [12, 20, 30]})
    led.restore_led()
    lamp.dispatch.assert_called_once_with("solid", (12, 20, 30))
