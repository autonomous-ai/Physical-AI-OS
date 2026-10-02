"""Resting LED uses device presets without undoing an explicit off command."""
from unittest.mock import Mock

import pytest

from hal import app_state as state, presets
from hal.drivers.harness import led as harness_led
from hal.models import LEDOffRequest
from hal.routes import led


@pytest.fixture
def lamp(monkeypatch, tmp_path, lamp_presets):
    for key, value in lamp_presets["ambient_led"]["resting"].items():
        monkeypatch.setitem(presets.AMBIENT_RESTING_LED, key, value)
    monkeypatch.setattr(state, "rgb_service", Mock())
    monkeypatch.setattr(state, "sensing_service", None)
    monkeypatch.setattr(state, "_user_led_state", None)
    monkeypatch.setattr(state, "_LED_STATE_PATH", str(tmp_path / "led.json"))
    for flag in ("_sleeping", "_tts_speaking", "_music_playing", "_thinking_cue_active"):
        monkeypatch.setattr(state, flag, False)
    monkeypatch.setattr(state, "_restore_timer", None)
    monkeypatch.setattr(state, "_effect_thread", None)
    monkeypatch.setattr(state, "_effect_name", None)
    monkeypatch.setattr(state, "_effect_base_color", None)
    monkeypatch.setattr(state, "_effect_stop", Mock())
    monkeypatch.setattr(state, "_stop_current_effect", Mock())
    monkeypatch.setattr(state, "_mic_muted_led_owns_strip", lambda: False)
    monkeypatch.setattr(state, "_dismiss_mic_muted_led", Mock())
    monkeypatch.setattr(harness_led, "restore", lambda: False)
    monkeypatch.setattr(led, "_end_scene", Mock())
    # A solid preset must never create an effect worker.
    monkeypatch.setattr(state.threading, "Thread", Mock(side_effect=AssertionError("solid started a worker")))
    return state.rgb_service


def test_default_restores_solid_without_effect_thread(lamp, lamp_presets):
    led.restore_led()
    lamp.dispatch.assert_called_once_with("solid", tuple(lamp_presets["ambient_led"]["resting"]["color"]))
    assert state._user_led_state is None
    assert not state.led_should_stay_dark()


def test_off_survives_restore_and_sidecar_reload(lamp):
    led.turn_off_leds(LEDOffRequest())
    assert state._load_user_led_state() == {"type": "solid", "color": [0, 0, 0]}
    assert state.led_should_stay_dark()
    led.restore_led()
    lamp.dispatch.assert_called_once_with("solid", (0, 0, 0))


def test_transient_off_returns_to_device_default(lamp, lamp_presets):
    led.turn_off_leds(LEDOffRequest(transient=True))
    assert state._user_led_state is None
    led.restore_led()
    lamp.dispatch.assert_called_once_with("solid", tuple(lamp_presets["ambient_led"]["resting"]["color"]))


@pytest.mark.parametrize("flag", ["_tts_speaking", "_music_playing"])
def test_ambient_restore_does_not_interrupt_playback(lamp, monkeypatch, flag):
    monkeypatch.setattr(state, flag, True)
    led.restore_led()
    lamp.dispatch.assert_not_called()


def test_explicit_color_survives_ambient_restore(lamp, monkeypatch):
    monkeypatch.setattr(state, "_user_led_state", {"type": "solid", "color": [12, 20, 30]})
    led.restore_led()
    lamp.dispatch.assert_called_once_with("solid", (12, 20, 30))


@pytest.mark.parametrize("emotion", [None, "thinking", "acknowledge"])
def test_speaking_wave_preserves_dim_display_color(lamp, monkeypatch, emotion, lamp_presets):
    monkeypatch.setattr(state, "_effect_base_color", None)
    monkeypatch.setattr(state, "display_service", None)
    led.restore_led()
    expected = tuple(lamp_presets["ambient_led"]["resting"]["color"])
    if emotion:
        expected = (2, 0, 3) if emotion == "thinking" else (0, 3, 0)
        monkeypatch.setitem(state.EMOTION_PRESETS, emotion, {"color": list(expected)})
        state._apply_emotion_led_display(emotion)
    worker = Mock()
    monkeypatch.setattr(state.threading, "Thread", worker)
    state._on_tts_speak_start()
    assert worker.call_args.kwargs["args"][1] == expected
    worker.return_value.start.assert_called_once()


def test_speaking_color_before_first_restore_uses_dim_default(lamp, monkeypatch, lamp_presets):
    monkeypatch.setattr(state, "_effect_base_color", None)
    assert state._get_current_led_color() == tuple(lamp_presets["ambient_led"]["resting"]["color"])


def test_explicit_off_blocks_stale_emotion_wave_color(lamp, monkeypatch):
    monkeypatch.setattr(state, "_effect_base_color", (2, 0, 3))
    monkeypatch.setattr(state, "_user_led_state", {"type": "solid", "color": [0, 0, 0]})
    assert state._get_current_led_color() == (0, 0, 0)
