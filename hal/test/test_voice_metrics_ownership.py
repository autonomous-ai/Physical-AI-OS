"""Regression coverage for mute and queued playback attribution."""

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from hal.telemetry import voice_metrics
from hal.test.test_voice_metrics import kpi  # noqa: F401
from hal.test.test_tts_playback_tracking import _drain_service, _tapped


@pytest.mark.parametrize("owner", ["", "run:unrelated", "interaction:missing"])
def test_unknown_muted_playback_does_not_exclude_newest_command(kpi, owner):
    iid = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(iid, "new-command")
    voice_metrics.playback_muted(owner)
    voice_metrics._close_interaction(iid)
    row = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert row["eligible"] is True
    assert row["outcome"] == "no_ack"
    assert row["exclusion_reason"] == ""


@pytest.mark.parametrize(
    "initial_feedback,initial_interruptible,queued_feedback,queued_interruptible,expected_kind,answer",
    [
        (False, True, True, False, "agent_reply", 900),
        (True, False, False, True, "waiting_audio", None),
        (True, False, False, False, "system_audio", None),
    ],
)
def test_actual_queued_request_classifies_its_own_segment(
    kpi, monkeypatch, tmp_path, initial_feedback, initial_interruptible,
    queued_feedback, queued_interruptible, expected_kind, answer,
):
    writes, fired = [], []
    service = _drain_service(writes, fired)
    service._backend = SimpleNamespace(available=True)
    service._speaker_muted = lambda: False
    service._speaking = True
    service._queue_request_lock = threading.Lock()
    service._lock = threading.Lock()
    service._lock.acquire()
    service._tts_cache_path = lambda text: tmp_path / "missing.wav"
    service._native_mode = False
    service._realtime_feedback = initial_feedback
    service._interruptible = initial_interruptible
    service._begin_playback("run:stream-opener")
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    iid = voice_metrics.speech_end("silence_clock")
    voice_metrics.bind_run(iid, "queued-run")
    service._on_playback_audio = lambda owner: voice_metrics.playback_audio(owner, service)

    assert service.speak_queue(
        "queued segment", turn_id="queued-run", realtime_feedback=queued_feedback,
        interruptible=queued_interruptible,
    )
    item = service._pending_queue[0]
    item.frame_queue.put(np.zeros(16, dtype=np.float32))
    item.frame_queue.put(None)
    kpi.clock.advance(900)
    service._drain_pending_queue(_tapped(service, writes))
    voice_metrics._close_interaction(iid)
    row = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert writes
    assert row["ack_latency_ms"] == 900
    assert row["ack_kind"] == expected_kind
    assert row["answer_latency_ms"] == answer
    assert service.realtime_feedback == initial_feedback
    assert service.interruptible == initial_interruptible
