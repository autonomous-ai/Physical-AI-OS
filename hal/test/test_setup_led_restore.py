"""Setup completion must not convert a system cue into an explicit user OFF."""
import json
from unittest.mock import Mock

import pytest

from hal import app_state as state, presets, resting_led
from hal.models import LEDOffRequest, LEDSolidRequest, LEDStatusRequest
from hal.routes import led
from hal.test.test_resting_led import device, strip  # noqa: F401


@pytest.fixture
def setup_strip(strip, monkeypatch):
    monkeypatch.setattr(led, "_end_scene", Mock())
    monkeypatch.setattr(state, "_dismiss_mic_muted_led", Mock())
    monkeypatch.setitem(led.STATUS_LED_PRESETS, "setup", {
        "effect": "solid", "color": [255, 255, 255], "speed": 1.0,
    })
    return strip


@pytest.mark.parametrize("custom", [False, True])
def test_setup_off_then_restore_returns_to_resting(setup_strip, custom):
    resting_led.init()
    if custom:
        resting_led.set_choice("custom", [7, 7, 7])
    expected = tuple(presets.AMBIENT_RESTING_LED["color"])
    led.set_led_status(LEDStatusRequest(state="setup"))
    assert state._user_led_state["source"] == "status:setup"
    # Provenance survives the same-boot sidecar used across HAL restarts.
    with open(state._LED_STATE_PATH) as f:
        assert json.load(f)["state"]["source"] == "status:setup"
    led.turn_off_leds(LEDOffRequest())
    assert state._user_led_state is None
    setup_strip.dispatch.assert_called_with("solid", expected)
    led.restore_led()
    setup_strip.dispatch.assert_called_with("solid", expected)
    assert tuple(resting_led.snapshot()["effective"]["color"]) == expected


def test_user_white_matching_setup_still_turns_off(setup_strip):
    led.set_led_status(LEDStatusRequest(state="setup"))
    led.set_led_solid(LEDSolidRequest(color=[255, 255, 255]))
    assert "source" not in state._user_led_state
    led.turn_off_leds(LEDOffRequest())
    led.restore_led()
    assert state._user_led_state == {"type": "solid", "color": [0, 0, 0]}
    setup_strip.dispatch.assert_called_with("solid", (0, 0, 0))


def test_user_off_after_setup_stays_off(setup_strip):
    led.set_led_status(LEDStatusRequest(state="setup"))
    led.turn_off_leds(LEDOffRequest())
    led.turn_off_leds(LEDOffRequest())
    led.restore_led()
    assert state._user_led_state == {"type": "solid", "color": [0, 0, 0]}
    setup_strip.dispatch.assert_called_with("solid", (0, 0, 0))


def test_transient_off_does_not_discard_setup(setup_strip):
    led.set_led_status(LEDStatusRequest(state="setup"))
    led.turn_off_leds(LEDOffRequest(transient=True))
    assert state._user_led_state["source"] == "status:setup"
    led.restore_led()
    setup_strip.dispatch.assert_called_with("solid", (255, 255, 255))


def test_setup_completion_preserves_resting_off_choice(setup_strip):
    resting_led.set_choice("off")
    led.set_led_status(LEDStatusRequest(state="setup"))
    led.turn_off_leds(LEDOffRequest())
    assert state._user_led_state is None
    assert resting_led.snapshot()["mode"] == "off"
    setup_strip.clear.assert_called()


def test_setup_status_while_sleeping_does_not_change_saved_state(setup_strip, monkeypatch):
    monkeypatch.setattr(state, "_sleeping", True)
    saved = dict(state._user_led_state)
    led.set_led_status(LEDStatusRequest(state="setup"))
    assert state._user_led_state == saved
    setup_strip.dispatch.assert_not_called()
