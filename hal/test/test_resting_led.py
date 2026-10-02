"""Owner's resting LED choice layers over the device preset and survives restarts."""
import json
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from hal import app_state as state, presets, resting_led
from hal.drivers.harness import led as harness_led
from hal.models import LEDRestingPreviewRequest, LEDRestingRequest
from hal.routes import led


@pytest.fixture
def device(monkeypatch, tmp_path, lamp_presets):
    """A device using the Lamp resting preset."""
    monkeypatch.setattr(resting_led.config, "RESTING_LED_PATH", str(tmp_path / "resting_led.json"))
    saved = dict(presets.AMBIENT_RESTING_LED)
    presets.AMBIENT_RESTING_LED.clear()
    presets.AMBIENT_RESTING_LED.update(lamp_presets["ambient_led"]["resting"])
    monkeypatch.setattr(resting_led, "_device_default", None)
    monkeypatch.setattr(resting_led, "_choice", {"mode": "default"})
    yield tmp_path / "resting_led.json"
    presets.AMBIENT_RESTING_LED.clear()
    presets.AMBIENT_RESTING_LED.update(saved)


@pytest.fixture
def strip(device, monkeypatch, tmp_path):
    monkeypatch.setattr(state, "rgb_service", Mock())
    monkeypatch.setattr(state, "sensing_service", None)
    monkeypatch.setattr(state, "_user_led_state", {"type": "solid", "color": [0, 0, 0]})
    monkeypatch.setattr(state, "_LED_STATE_PATH", str(tmp_path / "led.json"))
    for flag in ("_sleeping", "_tts_speaking", "_music_playing", "_thinking_cue_active"):
        monkeypatch.setattr(state, flag, False)
    monkeypatch.setattr(state, "_restore_timer", None)
    monkeypatch.setattr(state, "_stop_current_effect", Mock())
    monkeypatch.setattr(state, "_mic_muted_led_owns_strip", lambda: False)
    monkeypatch.setattr(harness_led, "restore", lambda: False)
    return state.rgb_service


def test_custom_choice_rewrites_resting_look_in_place_and_persists(device, lamp_presets):
    reference = presets.AMBIENT_RESTING_LED
    snap = resting_led.set_choice("custom", [8, 4, 1])
    assert reference is presets.AMBIENT_RESTING_LED
    assert reference == {"effect": "solid", "color": [8, 4, 1]}
    assert snap["default"] == lamp_presets["ambient_led"]["resting"]
    assert json.loads(device.read_text()) == {"mode": "custom", "color": [8, 4, 1]}


def test_saved_choice_reapplies_after_restart(device, lamp_presets):
    device.write_text(json.dumps({"mode": "custom", "color": [12, 9, 6]}))
    resting_led.init()
    assert presets.AMBIENT_RESTING_LED["color"] == [12, 9, 6]
    assert resting_led.snapshot()["default"]["color"] == lamp_presets["ambient_led"]["resting"]["color"]


def test_off_and_black_custom_keep_strip_dark(device):
    resting_led.set_choice("off")
    assert presets.ambient_resting_is_dark()
    assert resting_led.set_choice("custom", [0, 0, 0])["mode"] == "off"


def test_default_returns_to_device_preset(device, lamp_presets):
    resting_led.init()
    resting_led.set_choice("custom", [8, 4, 1])
    resting_led.set_choice("default")
    assert presets.AMBIENT_RESTING_LED == lamp_presets["ambient_led"]["resting"]


def test_unreadable_file_falls_back_to_default(device, lamp_presets):
    device.write_text("{not json")
    resting_led.init()
    assert resting_led.snapshot()["mode"] == "default"
    assert presets.AMBIENT_RESTING_LED["color"] == lamp_presets["ambient_led"]["resting"]["color"]


@pytest.mark.parametrize("color", [None, [1, 2], [1, 2, 256], [1, 2, -1], [1.5, 2, 3], [True, 2, 3]])
def test_invalid_custom_color_is_rejected(device, color):
    with pytest.raises(ValueError):
        resting_led.set_choice("custom", color)


def test_put_clears_explicit_off_and_shows_new_look(strip):
    snap = led.set_led_resting(LEDRestingRequest(mode="custom", color=[8, 4, 1]))
    assert snap["effective"]["color"] == [8, 4, 1]
    assert state._user_led_state is None
    strip.dispatch.assert_called_once_with("solid", (8, 4, 1))


def test_put_rejects_bad_color(strip):
    with pytest.raises(HTTPException) as e:
        led.set_led_resting(LEDRestingRequest(mode="custom", color=[300, 0, 0]))
    assert e.value.status_code == 400


def test_put_while_asleep_saves_without_painting(strip, monkeypatch):
    monkeypatch.setattr(state, "_sleeping", True)
    led.set_led_resting(LEDRestingRequest(mode="custom", color=[8, 4, 1]))
    assert presets.AMBIENT_RESTING_LED["color"] == [8, 4, 1]
    strip.dispatch.assert_not_called()


@pytest.fixture
def no_timer(monkeypatch):
    """Capture preview timers instead of letting them fire on their own."""
    timers = []

    class FakeTimer:
        def __init__(self, interval, fn):
            self.interval, self.fn, self.cancelled, self.daemon = interval, fn, False, False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(led.threading, "Timer", FakeTimer)
    monkeypatch.setattr(led, "_preview_timer", None)
    monkeypatch.setattr(state, "_cancel_pending_restore", Mock())
    return timers


def test_preview_paints_without_saving(strip, no_timer, device):
    reply = led.preview_led_resting(LEDRestingPreviewRequest(color=[20, 0, 10]))
    assert reply == {"status": "ok", "painted": True}
    strip.dispatch.assert_called_once_with("solid", (20, 0, 10))
    assert presets.AMBIENT_RESTING_LED["color"] == [5, 4, 3]
    assert not device.exists()


def test_preview_expiry_restores_saved_look(strip, no_timer, monkeypatch):
    monkeypatch.setattr(state, "_user_led_state", None)
    led.preview_led_resting(LEDRestingPreviewRequest(color=[20, 0, 10]))
    led.preview_led_resting(LEDRestingPreviewRequest(color=[30, 0, 10]))
    first, latest = no_timer
    assert first.cancelled and latest.interval == led.RESTING_PREVIEW_HOLD_S
    first.fn()  # a superseded timer must not repaint
    assert strip.dispatch.call_count == 2
    latest.fn()
    strip.dispatch.assert_called_with("solid", (5, 4, 3))


def test_save_cancels_pending_preview(strip, no_timer):
    led.preview_led_resting(LEDRestingPreviewRequest(color=[20, 0, 10]))
    led.set_led_resting(LEDRestingRequest(mode="custom", color=[8, 4, 1]))
    assert no_timer[0].cancelled
    strip.dispatch.assert_called_with("solid", (8, 4, 1))


@pytest.mark.parametrize("flag", ["_sleeping", "_tts_speaking", "_music_playing", "mic_muted"])
def test_preview_never_cuts_into_sleep_speech_music_or_mic_privacy(strip, no_timer, monkeypatch, flag):
    if flag == "mic_muted":
        monkeypatch.setattr(state, "_mic_muted_led_owns_strip", lambda: True)
    else:
        monkeypatch.setattr(state, flag, True)
    assert led.preview_led_resting(LEDRestingPreviewRequest(color=[20, 0, 10]))["painted"] is False
    strip.dispatch.assert_not_called()
    assert no_timer == []


def test_preview_rejects_bad_color(strip, no_timer):
    with pytest.raises(HTTPException) as e:
        led.preview_led_resting(LEDRestingPreviewRequest(color=[1, 2]))
    assert e.value.status_code == 400
