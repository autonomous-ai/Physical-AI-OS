"""The body returns to idle after a direct move, unless it is owned or already playing."""

import threading
from unittest import mock

import hal.app_state as app_state
from hal.drivers.tracking import body


class _Svc:
    def __init__(self, **flags):
        self.idle_recording = "idle"
        self.dispatched = []
        self._tracking_active = flags.get("tracking", False)
        self._hold_mode = flags.get("hold", False)
        self._zero_mode = flags.get("zero", False)
        # None = parked by a direct move.
        self._current_recording = flags.get("recording", None)

    def dispatch(self, cmd, payload):
        self.dispatched.append((cmd, payload))


def _with_service(monkeypatch, svc):
    monkeypatch.setattr(app_state, "animation_service", svc, raising=False)
    body.cancel_pending_release()


def test_a_parked_body_is_handed_back_to_idle(monkeypatch):
    svc = _Svc()
    _with_service(monkeypatch, svc)
    assert body.release_to_idle("test") is True
    assert svc.dispatched == [("play", "idle")]


def test_it_leaves_the_body_alone_when_something_else_owns_it(monkeypatch):
    for flag in ("tracking", "hold", "zero"):
        svc = _Svc(**{flag: True})
        _with_service(monkeypatch, svc)
        assert body.release_to_idle("test") is False, flag
        assert svc.dispatched == [], flag


def test_it_does_not_interrupt_a_recording_that_started_meanwhile(monkeypatch):
    svc = _Svc(recording="happy")
    _with_service(monkeypatch, svc)
    assert body.release_to_idle("test") is False
    assert svc.dispatched == []


# Gaze's test double has no _current_recording at all — absent means parked.
def test_a_service_without_the_attribute_counts_as_parked(monkeypatch):
    svc = _Svc()
    del svc._current_recording
    _with_service(monkeypatch, svc)
    assert body.release_to_idle("test") is True


def test_a_missing_service_is_not_an_error(monkeypatch):
    _with_service(monkeypatch, None)
    assert body.release_to_idle("test") is False


def test_a_dispatch_failure_is_logged_not_raised(monkeypatch):
    svc = _Svc()
    svc.dispatch = mock.Mock(side_effect=RuntimeError("bus busy"))
    _with_service(monkeypatch, svc)
    assert body.release_to_idle("test") is False


def test_a_delayed_release_fires_after_the_window(monkeypatch):
    svc = _Svc()
    _with_service(monkeypatch, svc)
    t = body.release_to_idle_later(0.05, "test")
    assert svc.dispatched == []
    t.join(timeout=2.0)
    assert svc.dispatched == [("play", "idle")]


def test_delay_zero_releases_now_without_a_thread(monkeypatch):
    svc = _Svc()
    _with_service(monkeypatch, svc)
    assert body.release_to_idle_later(0.0, "test") is None
    assert svc.dispatched == [("play", "idle")]


def test_scheduling_again_cancels_the_earlier_timer(monkeypatch):
    svc = _Svc()
    _with_service(monkeypatch, svc)
    first = body.release_to_idle_later(0.2, "first")
    second = body.release_to_idle_later(0.05, "second")
    second.join(timeout=2.0)
    first.join(timeout=1.0)
    assert svc.dispatched == [("play", "idle")]


def test_the_timer_is_a_daemon(monkeypatch):
    _with_service(monkeypatch, _Svc())
    t = body.release_to_idle_later(5.0, "test")
    try:
        assert isinstance(t, threading.Timer) and t.daemon
    finally:
        body.cancel_pending_release()
