"""os-server's look holds the body under its own owner and never drops a user's hold."""

import pytest
from pydantic import ValidationError

import hal.app_state as state
from hal.drivers.motors import hold
from hal.models import ServoHoldOwnerRequest
from hal.presets import SERVO_CMD_PLAY, SERVO_IDLE
from hal.routes import servo

LOOK = ServoHoldOwnerRequest(owner="look")


class _Body:
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
    monkeypatch.setattr(servo, "_svc", lambda: svc)
    monkeypatch.setattr(servo, "_sleep_servo_locked", lambda: False)
    yield svc
    servo.release_hold(LOOK)  # never leave a live expiry timer behind


def test_claim_and_release_look(body):
    servo.claim_hold(LOOK)
    assert hold.owners(body) == {"look"}
    assert body._hold_mode and not body._hold_explicit
    servo.release_hold(LOOK)
    assert hold.owners(body) == frozenset()
    assert not body._hold_mode


def test_releasing_look_keeps_an_explicit_hold(body):
    hold.claim(body, hold.EXPLICIT)
    servo.claim_hold(LOOK)
    servo.release_hold(LOOK)
    assert hold.owners(body) == {"explicit"}
    assert body._hold_explicit


def test_releasing_look_replays_a_parked_still_idle(body):
    servo.claim_hold(LOOK)
    state._still_idle_deferred = "thinking"
    servo.release_hold(LOOK)
    assert body.played == [(SERVO_CMD_PLAY, SERVO_IDLE)]
    assert state._still_idle_deferred is None


def test_a_parked_resume_waits_for_an_explicit_hold(body):
    hold.claim(body, hold.EXPLICIT)
    servo.claim_hold(LOOK)
    state._still_idle_deferred = "thinking"
    servo.release_hold(LOOK)
    assert body.played == []


def test_a_parked_resume_stays_off_a_sleeping_body(body, monkeypatch):
    monkeypatch.setattr(servo, "_sleep_servo_locked", lambda: True)
    servo.claim_hold(LOOK)
    state._still_idle_deferred = "thinking"
    servo.release_hold(LOOK)
    assert body.played == []
    assert not body._hold_mode


def test_an_unreleased_look_hold_expires(body):
    servo.claim_hold(LOOK)
    servo._expire_look_hold(body)
    assert not body._hold_mode


def test_only_the_look_owner_is_accepted():
    with pytest.raises(ValidationError):
        ServoHoldOwnerRequest(owner="explicit")
