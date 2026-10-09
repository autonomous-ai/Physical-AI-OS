"""Presence must restore the configured look, not its own default or emotion cache."""
import pytest

from hal import app_state as state, resting_led
from hal.drivers.sensing.presence_service import PresenceState, PresenseService
from hal.presets import RGB_CMD_SOLID
from hal.test.test_resting_led import device, strip  # noqa: F401


@pytest.mark.parametrize('origin', [PresenceState.IDLE, PresenceState.AWAY])
@pytest.mark.parametrize('color', [None, [7, 4, 2]])
def test_return_restores_resting_choice(strip, monkeypatch, lamp_presets, origin, color):
    monkeypatch.setattr(state, '_user_led_state', None)
    if color:
        resting_led.set_choice('custom', color)
    svc = PresenseService(strip)
    svc._state = origin
    svc.on_motion()
    expected = color or lamp_presets['ambient_led']['resting']['color']
    strip.dispatch.assert_called_with(RGB_CMD_SOLID, tuple(expected))


def test_return_restores_saved_user_color(strip, monkeypatch):
    monkeypatch.setattr(state, '_user_led_state', {'type': 'solid', 'color': [9, 6, 3]})
    svc = PresenseService(strip)
    svc._state = PresenceState.AWAY
    svc.on_motion()
    strip.dispatch.assert_called_once_with(RGB_CMD_SOLID, (9, 6, 3))


def test_return_keeps_explicit_off(strip):
    svc = PresenseService(strip)
    svc._state = PresenceState.AWAY
    svc.on_motion()
    strip.dispatch.assert_not_called()


def test_return_does_not_replace_speaking_wave(strip, monkeypatch):
    monkeypatch.setattr(state, '_user_led_state', None)
    monkeypatch.setattr(state, '_tts_speaking', True)
    svc = PresenseService(strip)
    svc._state = PresenceState.IDLE
    svc.on_motion()
    strip.dispatch.assert_not_called()


def test_idle_dims_resting_choice_instead_of_emotion(strip, monkeypatch):
    monkeypatch.setattr(state, '_user_led_state', None)
    monkeypatch.setattr(state, '_effect_base_color', (255, 180, 100))
    monkeypatch.setattr('hal.config.IDLE_BRIGHTNESS', 0.5)
    resting_led.set_choice('custom', [8, 4, 2])
    PresenseService(strip)._dim_light()
    strip.dispatch.assert_called_once_with(RGB_CMD_SOLID, (4, 2, 1))
