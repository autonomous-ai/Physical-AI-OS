"""A still emotion's idle resume never moves a held body, and an aim cancels it."""

from unittest.mock import Mock

import pytest

import hal.app_state as state
from hal.drivers.motors import hold
from hal.models import ServoAimRequest
from hal.presets import SERVO_CMD_PLAY, SERVO_IDLE
from hal.routes import emotion, servo


class _Body:
    """The hold flags plus a record of what was played."""

    def __init__(self):
        self._hold_mode = False
        self._hold_explicit = False
        self.played = []

    def ensure_running(self):
        pass

    def dispatch(self, cmd, payload):
        self.played.append((cmd, payload))


@pytest.fixture
def body(monkeypatch):
    svc = _Body()
    monkeypatch.setattr(state, "animation_service", svc, raising=False)
    monkeypatch.setattr(state, "_active_scene", None)
    monkeypatch.setattr(state, "_current_emotion", "thinking")
    monkeypatch.setattr(state, "_still_idle_deferred", None)
    return svc


def test_an_unheld_body_resumes_idle(body):
    emotion._resume_idle_after_still(body, "thinking")
    assert body.played == [(SERVO_CMD_PLAY, SERVO_IDLE)]


def test_a_held_body_stays_put_and_parks_the_resume(body):
    hold.claim(body, hold.EXPLICIT)
    emotion._resume_idle_after_still(body, "thinking")
    assert body.played == []
    assert state._still_idle_deferred == "thinking"


def test_a_newer_emotion_owns_the_body(body, monkeypatch):
    monkeypatch.setattr(state, "_current_emotion", "happy")
    emotion._resume_idle_after_still(body, "thinking")
    assert body.played == []
    assert state._still_idle_deferred is None


def test_cancel_drops_the_timer_and_the_parked_resume(monkeypatch):
    timer = Mock()
    monkeypatch.setattr(state, "_still_idle_timer", timer)
    monkeypatch.setattr(state, "_still_idle_deferred", "thinking")
    state.cancel_still_idle_timer()
    timer.cancel.assert_called_once()
    assert state._still_idle_timer is None
    assert state._still_idle_deferred is None


def test_an_aim_cancels_the_pending_still_timer(body, monkeypatch):
    timer = Mock()
    monkeypatch.setattr(state, "_still_idle_timer", timer)
    body.get_positions = lambda: {}
    body.aim = lambda direction, duration, current, policy: {"base_yaw.pos": 30.0}
    monkeypatch.setattr(servo, "_sleep_servo_locked", lambda: False)
    monkeypatch.setattr(servo, "_svc_connected", lambda: body)
    monkeypatch.setattr(servo, "_pose_reply", lambda svc, direction, positions: {"status": "ok"})
    servo.aim_servo(ServoAimRequest(direction="left"))
    timer.cancel.assert_called_once()
