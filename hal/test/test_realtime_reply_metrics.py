"""Realtime text replies retain answer metadata through every TTS path."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from hal.drivers.voice._internal import realtime_turn
from hal.realtime.models import TextOutput
from hal.telemetry import voice_metrics
from hal.test.test_tts_playback_tracking import _playback_service, _tapped, _wav
from hal.test.test_voice_metrics import kpi  # noqa: F401 -- shared clock/transport fixture


@pytest.fixture
def playback(monkeypatch, tmp_path):
    """Run workers explicitly; only synthesis and the audio device are fake."""
    workers = []

    class Worker:
        def __init__(self, target, args=(), **kwargs):
            self.target, self.args = target, args

        def start(self):
            workers.append(self)

        def run(self):
            self.target(*self.args)

    monkeypatch.setattr(threading, "Thread", Worker)
    writes = []
    service = _playback_service(tmp_path, writes, [])
    service._backend = SimpleNamespace(available=True, volume_boost=1.0)
    service._lock = threading.Lock()
    service._queue_request_lock = threading.Lock()
    service._pending_queue_lock = threading.Lock()
    service._pending_queue = []
    service._speaking = False
    service._interruptible = False
    service._realtime_feedback = False
    service._native_mode = False
    service._on_speak_end = None
    service._speaker_muted = lambda: False
    service._tts_cache_path = lambda text: tmp_path / "missing.wav"
    service._on_playback_audio = lambda owner: voice_metrics.playback_audio(owner, service)

    def synthesize(text):
        try:
            _tapped(service, writes).write(np.zeros(16, dtype=np.float32))
        finally:
            service._speaking = False
            service._lock.release()

    def presynthesize(item):
        item.frame_queue.put(np.zeros(16, dtype=np.float32))
        item.frame_queue.put(None)

    service._speak_sync = synthesize
    service._pre_synth_pending = presynthesize
    return service, workers, writes


@pytest.mark.parametrize("method,cache", [
    ("speak", False), ("speak_queue", False), ("speak_cached", True),
    ("speak", True), ("speak_queue", True),
])
def test_realtime_answer_is_measured_without_feedback(
    kpi, playback, tmp_path, method, cache,
):
    service, workers, writes = playback
    iid = voice_metrics.speech_end("silence_clock")
    if cache:
        wav = _wav(tmp_path)
        service._tts_cache_path = lambda text: wav

    assert getattr(service, method)("Hello there.", turn_id=iid, realtime_reply=True)
    assert not writes
    kpi.clock.advance(900)
    workers.pop(0).run()
    kpi.close_all()

    params = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert writes
    assert service.realtime_feedback is False
    assert params["ack_kind"] == "realtime_tts"
    assert params["ack_modality"] == "spoken_answer_realtime"
    assert params["ack_latency_ms"] == 900
    assert params["answer_latency_ms"] == 900
    assert params["answer_kind"] == "realtime_tts"


def test_busy_queue_keeps_each_segments_realtime_answer_metadata(kpi, playback):
    service, workers, writes = playback
    iid = voice_metrics.speech_end("silence_clock")
    service._begin_playback("unrelated-owner")
    service._speaking = True
    service._lock.acquire()

    assert service.speak_queue("Hello there.", turn_id=iid, realtime_reply=True)
    assert service.speak_queue("System notice.", turn_id=iid)
    for worker in workers:
        worker.run()
    kpi.clock.advance(900)
    service._drain_pending_queue(_tapped(service, writes))
    service._lock.release()
    kpi.close_all()

    params = kpi.one(voice_metrics.EVENT_INTERACTION)
    assert len(writes) == 2
    assert params["answer_latency_ms"] == 900
    assert params["answer_kind"] == "realtime_tts"
    assert service.realtime_reply is False
    assert service.realtime_feedback is False


@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("cap,chunks,spoken", [
    (100, ["Hello there,", " I am here.", "More to say"],
     ["Hello there,", "I am here.", "More to say"]),
    (100, ["Hello there.", "Welcome back."], ["Hello there.", "Welcome back."]),
    (100, ["Hello there"], ["Hello there"]),
    (0, ["Hello there,", " I am here.", "Welcome back."],
     ["Hello there, I am here.", "Welcome back."]),
    (100, ["Hello there, I am here.", "Welcome back."],
     ["Hello there, I am here.", "Welcome back."]),
])
def test_realtime_turn_marks_every_reply_segment(monkeypatch, cap, chunks, spoken, busy):
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_NATIVE_AUDIO", False)
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_PROVIDER", "openai")
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_FIRST_CHUNK_MAX_CHARS", cap)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_start", lambda: None)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_clear", lambda: None)
    monkeypatch.setattr(realtime_turn, "_reply_language_name", lambda: "English")
    monkeypatch.setattr(realtime_turn, "_WaitFiller", Mock())
    realtime = Mock(available=True)
    tts = Mock()
    tts.speak.return_value = not busy

    def outputs():
        for index, text in enumerate(chunks):
            yield TextOutput(text=text)
            if cap == 0 and index == 0:
                # A comma alone must not start a separate provider request.
                tts.speak.assert_not_called()
                tts.speak_queue.assert_not_called()
            if cap == 0 and index == 1:
                # The first sentence starts before the model sends sentence 2.
                tts.speak.assert_called_once_with(
                    spoken[0], turn_id="vi-test", realtime_reply=True,
                )
                assert tts.speak_queue.call_count == int(busy)

    realtime.stream_output.return_value = outputs()

    result = realtime_turn.run_realtime_turn(
        realtime, tts, lambda text: text, "Hello", [object()], 1.0,
        interaction_id="vi-test",
    )

    assert result.handled
    calls = tts.method_calls
    assert calls[0].args == (spoken[0],)
    accepted = [call.args[0] for call in calls if call[0] == "speak_queue" or not busy]
    assert accepted == spoken
    for call in calls:
        assert call.kwargs["turn_id"] == "vi-test"
        assert call.kwargs["realtime_reply"] is True
        assert call.kwargs.get("realtime_feedback", False) is False


@pytest.mark.parametrize("chunks,ready,expected", [
    (["I'm right here! [cheerfully]"], [True], "I'm right here!"),
    (["I'm right here! [cheer", "fully]"], [False, True], "I'm right here!"),
    (["I'm right here! [HW:/led/off:{}]"], [True], "I'm right here!"),
    (["I'm right here! NO_REPLY"], [True], "I'm right here!"),
    (["I'm right here [cheerfully]"], [False], "I'm right here"),
    (["[cheerfully]"], [False], None),
])
def test_tagged_reply_flushes_before_provider_finishes(monkeypatch, chunks, ready, expected):
    from hal.drivers.voice.voice_service import VoiceService

    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_NATIVE_AUDIO", False)
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_PROVIDER", "openai")
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_FIRST_CHUNK_MAX_CHARS", 100)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_start", lambda: None)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_clear", lambda: None)
    monkeypatch.setattr(realtime_turn, "_reply_language_name", lambda: "English")
    monkeypatch.setattr(realtime_turn, "_WaitFiller", Mock())
    realtime, tts = Mock(available=True), Mock()
    tts.speak.return_value = True

    def outputs():
        for text, should_speak in zip(chunks, ready):
            yield TextOutput(text=text)
            # This runs while the provider is still waiting for tool/routing
            # completion. No terminal or iterator exhaustion has happened yet.
            assert tts.speak.called is should_speak
        if any(ready):
            tts.speak.assert_called_once_with(expected, turn_id="vi-tag", realtime_reply=True)

    realtime.stream_output.return_value = outputs()
    realtime_turn.run_realtime_turn(
        realtime, tts, VoiceService.strip_rt_markers, "Can you hear me over there",
        [object()], 2.0, interaction_id="vi-tag",
    )
    if expected is not None:
        tts.speak.assert_called_once_with(expected, turn_id="vi-tag", realtime_reply=True)
    else:
        tts.speak.assert_not_called()
    tts.speak_queue.assert_not_called()


def test_realtime_turn_reuses_a_filler_armed_before_the_handshake(monkeypatch):
    """The caller may arm the dead-air filler before the Gemini reconnect and
    hand it in; the turn must not create a second one (one filler per turn)."""
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_NATIVE_AUDIO", False)
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_PROVIDER", "openai")
    monkeypatch.setattr(realtime_turn, "_thinking_cue_start", lambda: None)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_clear", lambda: None)
    monkeypatch.setattr(realtime_turn, "_reply_language_name", lambda: "English")
    factory = Mock()
    monkeypatch.setattr(realtime_turn, "_WaitFiller", factory)
    realtime = Mock(available=True)
    realtime.stream_output.return_value = iter([TextOutput(text="Hello there.")])
    tts = Mock()
    tts.speak.return_value = True
    armed = Mock()

    result = realtime_turn.run_realtime_turn(
        realtime, tts, lambda text: text, "Can you hear me clearly over there today",
        [object()], 3.0, interaction_id="vi-test", wait_filler=armed,
    )

    assert result.handled
    factory.assert_not_called()
    armed.arm.assert_called_once()
    armed.cancel.assert_called()


def test_bundled_next_sentence_does_not_hold_completed_reply(monkeypatch):
    from hal.drivers.voice.voice_service import VoiceService
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_ENABLED", True)
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_NATIVE_AUDIO", False)
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_PROVIDER", "openai")
    monkeypatch.setattr(realtime_turn.hal_config, "REALTIME_FIRST_CHUNK_MAX_CHARS", 0)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_start", lambda: None)
    monkeypatch.setattr(realtime_turn, "_thinking_cue_clear", lambda: None)
    monkeypatch.setattr(realtime_turn, "_reply_language_name", lambda: "English")
    monkeypatch.setattr(realtime_turn, "_WaitFiller", Mock())
    realtime, tts = Mock(available=True), Mock()
    tts.speak.return_value = True

    def outputs():
        yield TextOutput(text="I am right here. Let me")
        tts.speak.assert_called_once_with("I am right here.", turn_id="vi-prefix", realtime_reply=True)
        yield TextOutput(text=" help you.")
        assert tts.speak_queue.call_args.args == ("Let me help you.",)

    realtime.stream_output.return_value = outputs()
    realtime_turn.run_realtime_turn(
        realtime, tts, VoiceService.strip_rt_markers, "Can you hear me over there",
        [object()], 2.0, interaction_id="vi-prefix",
    )
    assert tts.speak_queue.call_count == 1
