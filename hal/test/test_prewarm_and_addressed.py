"""Session prewarm on presence/gaze, and the addressed-evidence line for the model."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import hal.app_state as state
from hal import config as hal_config
from hal.drivers.tracking import gaze
from hal.drivers.voice._internal import prewarm, turn_admission
from hal.drivers.voice._internal.realtime_turn import build_turn_context


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    prewarm.reset_for_test()
    monkeypatch.setattr(state, "voice_service", None, raising=False)
    yield
    prewarm.reset_for_test()


def test_prewarm_resumes_a_parked_session_and_rate_limits(monkeypatch):
    realtime = Mock()
    realtime.prewarm.return_value = True
    monkeypatch.setattr(state, "voice_service", SimpleNamespace(realtime=realtime), raising=False)
    assert prewarm.prewarm_realtime("presence.enter", now=100.0)
    # A burst of gaze samples right after must not hammer the orchestrator.
    assert not prewarm.prewarm_realtime("gaze", now=100.5)
    assert prewarm.prewarm_realtime("gaze", now=100.0 + prewarm.PREWARM_MIN_INTERVAL_S)
    assert realtime.prewarm.call_count == 2


def test_prewarm_is_quiet_without_a_voice_service():
    assert not prewarm.prewarm_realtime("presence.enter", now=200.0)


def test_prewarm_reports_false_when_nothing_was_parked(monkeypatch):
    realtime = Mock()
    realtime.prewarm.return_value = False
    monkeypatch.setattr(state, "voice_service", SimpleNamespace(realtime=realtime), raising=False)
    assert not prewarm.prewarm_realtime("gaze", now=300.0)


def test_a_facing_gaze_sample_prewarms(monkeypatch):
    realtime = Mock()
    realtime.prewarm.return_value = True
    monkeypatch.setattr(state, "voice_service", SimpleNamespace(realtime=realtime), raising=False)
    monkeypatch.setattr(hal_config, "GAZE_MIN_FACE_PX", 10.0)
    gaze.discard_samples()
    gaze.record_sample(0.0, 40.0, 0.0, now=10.0)      # facing the lamp
    assert realtime.prewarm.call_count == 1
    gaze.record_sample(None, 40.0, 0.0, now=20.0)     # no face measurement
    assert realtime.prewarm.call_count == 1
    gaze.discard_samples()


@pytest.mark.parametrize("kwargs, expect", [
    (dict(wake_word=True, window=False, question=False, facing=None), "Addressed: yes (wake phrase heard)"),
    (dict(wake_word=False, window=True, question=False, facing=False), "Addressed: yes (open conversation window)"),
    (dict(wake_word=False, window=False, question=True, facing=True),
     "Addressed: likely (answering your question; the user is facing you)"),
    (dict(wake_word=False, window=False, question=False, facing=True, known_voice="Dee"),
     "Addressed: likely (the user is facing you; known voice: Dee)"),
    (dict(wake_word=False, window=False, question=False, facing=False),
     "Addressed: unlikely (the user is facing away) — stay silent unless clearly spoken to"),
    (dict(wake_word=False, window=False, question=False, facing=None),
     "Addressed: unknown (no name, no face evidence, no open conversation) — stay silent unless clearly spoken to"),
])
def test_addressed_hint_states_the_evidence(kwargs, expect):
    assert turn_admission.addressed_hint(**kwargs) == expect


def test_turn_context_carries_the_hint():
    ctx = build_turn_context("Dee", addressed="Addressed: yes (wake phrase heard)")
    assert ctx.startswith("[TURN CONTEXT] ")
    assert "Current user: Dee" in ctx
    assert ctx.endswith("Addressed: yes (wake phrase heard)")
    assert "Addressed:" not in build_turn_context("Dee")


def test_facing_evidence_reads_the_gaze_window(monkeypatch):
    monkeypatch.setattr(hal_config, "GAZE_MIN_SAMPLES", 2)
    monkeypatch.setattr(hal_config, "GAZE_MIN_FACING_RATIO", 0.6)
    monkeypatch.setattr(gaze, "facing_ratio", lambda now=None: (0.0, 0))
    assert turn_admission.facing_evidence() is None
    monkeypatch.setattr(gaze, "facing_ratio", lambda now=None: (0.9, 4))
    assert turn_admission.facing_evidence() is True
    monkeypatch.setattr(gaze, "facing_ratio", lambda now=None: (0.2, 4))
    assert turn_admission.facing_evidence() is False
