"""Exercise the real HAL queue across LIVE/main speaker handoff."""
from types import SimpleNamespace
import threading
from unittest.mock import Mock

import numpy as np
import pytest

from hal import config
from hal.drivers.voice.voice_service import VoiceService
from hal.drivers.voice import voice_service
from hal.drivers.voice.tts import service as module
from hal.test.test_tts_turn_queue import _queue_service, _InlineThread
from hal.test.test_live_voice_metrics import _pump
from hal.test.test_voice_metrics import kpi  # noqa: F401 -- clock/transport fixture
from hal.realtime.models.output import TextOutput, UserSpeechOutput


def speaker(monkeypatch, tmp_path, native=False):
    monkeypatch.setattr(config, "LIVE_MODE", True)
    monkeypatch.setattr(module.threading, "Thread", _InlineThread)
    tts = _queue_service()
    tts._native_mode = native
    tts._realtime_reply = not native
    tts._speaking = True
    tts._lock.acquire()
    tts._stream_lock = threading.Lock()
    tts._device_rate = 24000
    tts._tts_cache_path = lambda _: tmp_path / "missing.wav"
    tts._on_speak_start = Mock()
    tts._on_speak_end = Mock()
    tts._speak_start_fired = True
    tts._note_playback_done = Mock()
    tts._register_drain_queue = Mock()
    tts._forget_drain_queues = Mock()
    tts._wake_drain_queues = Mock()
    writes = []
    stream = SimpleNamespace(write=lambda frame: writes.append(
        (tts._playback_owner, len(frame), tts.realtime_feedback, tts.native_mode)))
    tts._ensure_stream = lambda _: stream
    tts._pre_synth_pending = lambda item: (
        item.frame_queue.put(np.ones((8, 1))), item.frame_queue.put(None))
    voice = object.__new__(VoiceService)
    voice._tts = tts
    voice._live_running = True
    return tts, voice, writes, stream


def enqueue(tts, voice, text, seq=1):
    return tts.speak_queue(text, turn_id=f"main-{seq}", turn_seq=seq,
                           realtime_feedback=True,
                           defer_preemption=lambda: voice.live_speaker_busy)


@pytest.mark.parametrize("native", [False, True])
def test_busy_live_queues_main_without_stopping_then_plays_in_order(monkeypatch, tmp_path, native):
    tts, voice, writes, stream = speaker(monkeypatch, tmp_path, native)
    assert voice.live_active and voice.live_speaker_busy
    assert enqueue(tts, voice, "first")
    assert enqueue(tts, voice, "tail")
    assert not writes and not tts._stop_event.is_set()
    assert [item.text for item in tts._pending_queue] == ["first", "tail"]
    if native:
        tts.native_play_end("LIVE reply")
        assert not tts._lock.locked()
    else:
        tts._drain_pending_queue(stream)
    assert writes == [("run:main-1", 8, True, False)] * 2
    assert not voice.live_speaker_busy


@pytest.mark.parametrize("native", [False, True])
def test_stop_discards_waiting_main_before_handoff(monkeypatch, tmp_path, native):
    tts, voice, writes, stream = speaker(monkeypatch, tmp_path, native)
    enqueue(tts, voice, "must not play")
    tts.stop()
    if native:
        tts.native_play_end("interrupted LIVE")
    else:
        tts._drain_pending_queue(stream)
    assert writes == []
    assert tts._pending_queue == []


def test_new_main_turn_replaces_waiting_old_main_without_stopping_live(monkeypatch, tmp_path):
    tts, voice, writes, stream = speaker(monkeypatch, tmp_path)
    enqueue(tts, voice, "old", 1)
    enqueue(tts, voice, "new", 2)
    enqueue(tts, voice, "late old", 1)
    assert [item.text for item in tts._pending_queue] == ["new"]
    assert not tts._stop_event.is_set()
    tts._drain_pending_queue(stream)
    assert writes == [("run:main-2", 8, True, False)]


def test_property_distinguishes_open_mic_main_and_live_audio(monkeypatch, tmp_path):
    tts, voice, _, _ = speaker(monkeypatch, tmp_path)
    tts._speaking = False
    assert voice.live_active and not voice.live_speaker_busy
    tts._speaking = True
    tts._realtime_reply = False
    assert voice.live_active and not voice.live_speaker_busy
    tts._native_mode = True
    assert voice.live_speaker_busy
    voice._live_running = False
    assert not voice.live_active and not voice.live_speaker_busy


def test_stop_while_waiting_for_first_queued_frame_cannot_write_it(monkeypatch, tmp_path):
    tts, voice, writes, stream = speaker(monkeypatch, tmp_path)
    enqueue(tts, voice, "cancel me")
    item = tts._pending_queue[0]
    def cancelled_get(**kwargs):
        tts.stop()
        return np.ones((8, 1))
    item.frame_queue = SimpleNamespace(get=cancelled_get)
    tts._drain_pending_queue(stream)
    assert writes == []


def test_live_reset_preserves_main_queue_but_explicit_stop_still_cancels(monkeypatch, tmp_path):
    tts, voice, writes, _ = speaker(monkeypatch, tmp_path, native=True)
    enqueue(tts, voice, "keep after model reset")
    voice._live_stop_output()
    assert [item.text for item in tts._pending_queue] == ["keep after model reset"]
    tts.native_play_end()
    assert writes == [("run:main-1", 8, True, False)]


def test_explicit_stop_after_model_reset_cancels_preserved_main(monkeypatch, tmp_path):
    tts, voice, writes, _ = speaker(monkeypatch, tmp_path, native=True)
    enqueue(tts, voice, "must not resurrect")
    voice._live_stop_output()
    tts.stop()
    tts.native_play_end()
    assert writes == []
    assert not tts._lock.locked()


def test_last_moment_queue_arrival_is_drained_when_worker_releases(monkeypatch, tmp_path):
    tts, voice, writes, stream = speaker(monkeypatch, tmp_path)
    # Worker saw an empty queue; HTTP arrives just before it releases the playback lock.
    assert tts._drain_pending_queue(stream) == 0
    enqueue(tts, voice, "late arrival")
    tts._release_or_drain_live_queue()
    assert writes == [("run:main-1", 8, True, False)]
    assert not tts._lock.locked()


def test_retained_main_synthesis_survives_model_reset(monkeypatch, tmp_path):
    tts, voice, writes, _ = speaker(monkeypatch, tmp_path, native=True)
    tts._pre_synth_pending = lambda _: None
    enqueue(tts, voice, "still synthesizing")
    item = tts._pending_queue[0]
    voice._live_stop_output()
    assert tts._stop_event.is_set()
    tts._split_text_into_growing_sentence_chunks = lambda text: [text]
    tts._np = np
    tts._voice = tts._model = tts._instructions = ""
    tts._speed = 1.0
    tts._backend.sample_rate = 24000
    tts._backend.volume_boost = 1.0
    tts._backend.stream_pcm = lambda **kw: iter([np.ones(8, dtype=np.int16).tobytes()] * 2)
    module.TTSService._pre_synth_pending(tts, item)
    tts.native_play_end()
    assert writes == [("run:main-1", 8, True, False)] * 2


def test_new_speech_queued_while_stopped_worker_exits_is_resumed(monkeypatch, tmp_path):
    tts, voice, writes, _ = speaker(monkeypatch, tmp_path)
    tts.stop()
    assert tts.speak_queue("new realtime reply", realtime_reply=True)
    tts._release_or_drain_live_queue()
    assert writes == [("", 8, False, False)]
    assert not tts._lock.locked()


def test_main_ownership_is_set_before_waiting_for_first_frame(monkeypatch, tmp_path):
    tts, voice, writes, stream = speaker(monkeypatch, tmp_path)
    enqueue(tts, voice, "slow synthesis")
    item = tts._pending_queue[0]
    frames = iter([np.ones((8, 1)), None])
    def reset_during_wait(**kwargs):
        assert not voice.live_speaker_busy
        voice._live_stop_output()
        return next(frames)
    item.frame_queue = SimpleNamespace(get=reset_during_wait)
    tts._drain_pending_queue(stream)
    assert writes == [("run:main-1", 8, True, False)]


def test_preservation_is_published_before_waking_stopped_worker(monkeypatch, tmp_path):
    tts, voice, _, _ = speaker(monkeypatch, tmp_path, native=True)
    enqueue(tts, voice, "pending")
    observations = []
    tts._wake_drain_queues = lambda: observations.append(
        (tts._resume_pending_after_stop, len(tts._pending_queue)))
    voice._live_stop_output()
    assert observations == [(True, 1)]


def test_cached_live_reply_drains_waiting_main(monkeypatch, tmp_path):
    import wave
    tts, voice, writes, _ = speaker(monkeypatch, tmp_path)
    tts._np = np
    tts._backend.volume_boost = 1.0
    enqueue(tts, voice, "main after cached LIVE")
    path = tmp_path / "cached.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(np.zeros(32, dtype=np.int16).tobytes())
    tts._playback_owner = "live"
    tts._play_wav_inline(path)
    assert writes[-1] == ("run:main-1", 8, True, False)


@pytest.mark.parametrize("late_key,main_reply,preserved", [
    ("question", False, True),
    ("new-question", False, False),
    ("question", True, False),
])
def test_late_input_preserves_own_live_tail_but_new_input_stops(
    monkeypatch, tmp_path, kpi, late_key, main_reply, preserved,
):
    tts, _, writes, stream = speaker(monkeypatch, tmp_path)
    # Queue workers run inline here; history owns a long-lived background loop.
    monkeypatch.setattr(voice_service, "LiveHistory", Mock())
    tts.speak = Mock(return_value=False)
    head = "The campaign boosted foot traffic at the pilot pubs."
    tail = " Plus, they are working with the Vintners' Federation."

    def switch_owner():
        if main_reply:
            tts._realtime_reply = False
            tts._realtime_feedback = True

    _pump(monkeypatch, kpi, [([
        UserSpeechOutput(turn_id="question", endpoint_at=kpi.clock()),
        TextOutput(text=head, user_turn_id="question"),
        TextOutput(text=tail, user_turn_id="question"),
        switch_owner,
        UserSpeechOutput(turn_id=late_key, transcript="Tell me more about the campaign."),
    ], "question", True)], tts=tts)

    if preserved:
        assert "".join(item.text for item in tts._pending_queue) == head + tail
        assert not tts._stop_event.is_set()
        tts._drain_pending_queue(stream)
        assert len(writes) == 2
    else:
        assert tts._stop_event.is_set()
        assert not tts._pending_queue
        assert not writes
