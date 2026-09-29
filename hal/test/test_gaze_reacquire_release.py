"""A reacquire hold must not outlive the utterance it was made for."""

from unittest import mock

import hal.app_state as app_state
from hal.drivers.tracking import gaze


class _Svc:
    def __init__(self, **flags):
        self.idle_recording = "idle"
        self.dispatched = []
        self._tracking_active = flags.get("tracking", False)
        self._hold_mode = flags.get("hold", False)
        self._zero_mode = flags.get("zero", False)

    def dispatch(self, cmd, payload):
        self.dispatched.append((cmd, payload))


def _with_service(monkeypatch, svc):
    monkeypatch.setattr(app_state, "animation_service", svc, raising=False)


def test_the_hold_is_released_back_to_idle(monkeypatch):
    svc = _Svc()
    _with_service(monkeypatch, svc)

    gaze._release_reacquire_hold()

    assert svc.dispatched == [("play", "idle")]


def test_it_leaves_the_body_alone_when_something_else_owns_it(monkeypatch):
    for flag in ("tracking", "hold", "zero"):
        svc = _Svc(**{flag: True})
        _with_service(monkeypatch, svc)

        gaze._release_reacquire_hold()

        assert svc.dispatched == [], flag


def test_a_missing_service_is_not_an_error(monkeypatch):
    _with_service(monkeypatch, None)
    gaze._release_reacquire_hold()


def test_a_dispatch_failure_is_logged_not_raised(monkeypatch):
    svc = _Svc()
    svc.dispatch = mock.Mock(side_effect=RuntimeError("bus busy"))
    _with_service(monkeypatch, svc)

    gaze._release_reacquire_hold()


def test_speech_end_releases_the_hold_and_keeps_its_verdict(monkeypatch):
    svc = _Svc()
    _with_service(monkeypatch, svc)
    monkeypatch.setattr(gaze, "_speech_repoint_requested_t", 1.0)
    monkeypatch.setattr(gaze, "_check_speech", lambda *a, **k: True)

    assert gaze.on_speech_end() is True
    assert svc.dispatched == [("play", "idle")]


def test_speech_end_without_a_pending_reacquire_does_nothing(monkeypatch):
    svc = _Svc()
    _with_service(monkeypatch, svc)
    monkeypatch.setattr(gaze, "_speech_repoint_requested_t", 0.0)

    assert gaze.on_speech_end() is False
    assert svc.dispatched == []


def test_a_pending_hold_is_released_without_a_transcript(monkeypatch):
    svc = _Svc()
    _with_service(monkeypatch, svc)
    monkeypatch.setattr(gaze, "_speech_repoint_requested_t", 1.0)

    assert gaze.release_reacquire_hold_if_pending() is True
    assert svc.dispatched == [("play", "idle")]
    assert gaze._speech_repoint_requested_t == 0.0


def test_no_pending_reacquire_means_no_handover(monkeypatch):
    svc = _Svc()
    _with_service(monkeypatch, svc)
    monkeypatch.setattr(gaze, "_speech_repoint_requested_t", 0.0)

    assert gaze.release_reacquire_hold_if_pending() is False
    assert svc.dispatched == []


def test_it_does_not_interrupt_a_recording_that_started_meanwhile(monkeypatch):
    svc = _Svc()
    svc._current_recording = "happy"
    _with_service(monkeypatch, svc)

    gaze._release_reacquire_hold()

    assert svc.dispatched == []
