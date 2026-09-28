"""The playback-tracking hook on TTSService.

Two things these pin down:

* `on_speak_start` is NOT proof of playback — the cached path fires it before
  it has taken the stream lock or written a byte. The hook must key off the
  first frame that actually reaches the stream.
* There is exactly ONE measuring point, inside the stream wrapper every
  playback writes through. A new playback path cannot escape the metrics by
  forgetting to call anything.
"""

import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from hal.drivers.voice.tts.service import TTSService


class _FakeSd:
    """sounddevice stub: the wrapper only needs its error type."""

    class PortAudioError(Exception):
        pass


class _FakeDevice:
    """Stands in for sounddevice's OutputStream."""

    def __init__(self, recorder):
        self._recorder = recorder

    def write(self, data):
        self._recorder.append(len(data))


def _tapped(service, recorder):
    """The real wrapper, over a fake device — the same object every playback
    path writes through in production."""
    from hal.drivers.voice.tts.service import _WatchedStream

    return _WatchedStream(_FakeDevice(recorder), service)


def _playback_service(tmp_path, writes, fired, stopped=None):
    """TTSService shell with just enough state for _play_wav_inline."""
    service = object.__new__(TTSService)
    service._np = np
    service._sd = object()
    service._backend = None
    service._stream_lock = threading.Lock()
    service._stop_event = threading.Event()
    service._device_rate = 24000
    service._stream_rate = 24000
    service._on_speak_start = lambda: fired.append("speak_start")
    service._on_playback_audio = lambda owner: fired.append(("audio", owner))
    service._on_playback_done = lambda: (stopped or []).append("done")
    service._audio_written_fired = False
    service._playback_owner = ""
    service._stream = None
    service._sd = _FakeSd()
    service._ensure_stream = lambda rate: _tapped(service, writes)
    service._resample = lambda samples, src, dst: samples
    return service


def _wav(tmp_path: Path, seconds=0.2, rate=24000) -> Path:
    path = tmp_path / "phrase.wav"
    samples = (np.zeros(int(rate * seconds)) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return path


def test_audio_hook_fires_only_after_a_real_write(tmp_path):
    writes, fired = [], []
    service = _playback_service(tmp_path, writes, fired)
    service._begin_playback("run:run-7")

    service._play_wav_inline(_wav(tmp_path))

    assert writes, "test setup: expected frames to be written"
    kinds = [f for f in fired if isinstance(f, tuple)]
    assert kinds == [("audio", "run:run-7")]
    # on_speak_start fired first and is NOT the ack signal — that ordering is
    # exactly why the tracking hook exists.
    assert fired[0] == "speak_start"


def test_no_audio_hook_when_playback_is_stopped_before_writing(tmp_path):
    """Stopped between the callback and the first write: nothing was heard, so
    nothing may be reported as heard."""
    writes, fired = [], []
    service = _playback_service(tmp_path, writes, fired)
    service._begin_playback("run:run-7")
    service._stop_event.set()

    service._play_wav_inline(_wav(tmp_path))

    assert writes == []
    assert [f for f in fired if isinstance(f, tuple)] == []
    assert fired == ["speak_start"]


def test_audio_hook_fires_once_per_playback(tmp_path):
    writes, fired = [], []
    service = _playback_service(tmp_path, writes, fired)
    service._begin_playback("run:run-7")

    service._play_wav_inline(_wav(tmp_path, seconds=0.5))

    assert len(writes) > 1, "test setup: expected several blocks"
    assert len([f for f in fired if isinstance(f, tuple)]) == 1


def test_unclaimed_playback_reports_an_empty_owner(tmp_path):
    writes, fired = [], []
    service = _playback_service(tmp_path, writes, fired)
    service._begin_playback("")

    service._play_wav_inline(_wav(tmp_path))

    assert [f for f in fired if isinstance(f, tuple)] == [("audio", "")]


@pytest.mark.parametrize("cancelled", [False, True])
def test_gesture_chime_does_not_acknowledge_pending_voice_reply(tmp_path, monkeypatch, cancelled):
    """A tap's ping must not become the first frame of an unsynthesized reply,
    even after that reply's completion hook has run during cancellation.
    """
    from hal.telemetry import voice_metrics

    clock = [1000.0]
    monkeypatch.setattr(voice_metrics, "_now", lambda: clock[0])
    monkeypatch.setattr(threading.Timer, "start", lambda self: None)
    voice_metrics.reset_for_test()
    try:
        writes, fired = [], []
        service = _playback_service(tmp_path, writes, fired)
        service._backend = SimpleNamespace(available=True, volume_boost=1.0)
        service._speaker_muted = lambda: False
        service._ack_chime_cache = None
        service._native_mode = False
        service._realtime_feedback = True
        service._interruptible = False
        service._on_playback_audio = lambda owner: voice_metrics.playback_audio(owner, service)
        service._on_playback_done = voice_metrics.playback_end
        iid = voice_metrics.speech_end("silence_clock")
        voice_metrics.bind_run(iid, "pending-run")
        service._begin_playback("run:pending-run")
        if cancelled:
            service._stop_event.set()
            service._note_playback_done()

        clock[0] += 1
        assert service.play_ack_chime()
        assert writes, "The ping must still reach the audio device"
        assert not service._audio_written_fired
        assert voice_metrics._playing is None
        interaction = voice_metrics._interactions[iid]
        assert interaction.ack_latency_ms is None
        assert interaction.answer_latency_ms is None

        if not cancelled:
            clock[0] += 1
            service._play_wav_inline(_wav(tmp_path))
            assert interaction.ack_latency_ms == 2000
            assert interaction.answer_latency_ms == 2000
            assert interaction.ack_kind == voice_metrics.KIND_AGENT_REPLY
    finally:
        voice_metrics.reset_for_test()


@pytest.mark.parametrize("owner", ["run:abc", "interaction:vi-1", ""])
def test_begin_playback_rearms_the_hook_for_each_playback(tmp_path, owner):
    writes, fired = [], []
    service = _playback_service(tmp_path, writes, fired)
    wav = _wav(tmp_path)

    service._begin_playback("run:first")
    service._play_wav_inline(wav)
    service._begin_playback(owner)
    service._play_wav_inline(wav)

    owners = [f[1] for f in fired if isinstance(f, tuple)]
    assert owners == ["run:first", owner]


def _drain_service(writes, fired):  # noqa: D401
    """TTSService shell for the queued-segment drain path."""
    service = object.__new__(TTSService)
    service._np = np
    service._sd = _FakeSd()
    service._stream = None
    service._stream_rate = 24000
    service._stop_event = threading.Event()
    service._pending_queue_lock = threading.Lock()
    service._pending_queue = []
    service._on_playback_audio = lambda owner: fired.append((owner,))
    service._on_playback_done = lambda: None
    service._audio_written_fired = False
    service._playback_owner = ""
    service._last_spoken_text = ""
    return service


def _pending(text, owner):
    from hal.drivers.voice.tts.service import _PendingSpeech

    item = _PendingSpeech(text=text, interruptible=False, owner=owner)
    item.frame_queue.put(np.zeros(16, dtype=np.float32))
    item.frame_queue.put(None)
    return item


def test_queued_segment_reports_its_own_owner_not_the_stream_opener():
    """A sentence queued behind another turn plays on the stream that turn
    opened. Without re-arming per item it would be credited to the wrong turn.
    """
    writes, fired = [], []
    service = _drain_service(writes, fired)
    service._begin_playback("run:first-turn")     # who opened the stream
    service._audio_written_fired = True           # its own first frame already fired
    service._pending_queue = [_pending("second turn reply", "run:second-turn")]

    service._drain_pending_queue(_tapped(service, writes))

    assert fired == [("run:second-turn",)]


def test_each_queued_segment_fires_once():
    writes, fired = [], []
    service = _drain_service(writes, fired)
    service._begin_playback("run:a")
    service._pending_queue = [_pending("one", "run:a"), _pending("two", "run:b")]

    service._drain_pending_queue(_tapped(service, writes))

    assert [owner for (owner,) in fired] == ["run:a", "run:b"]


def test_cached_cue_batches_device_and_aec_work(monkeypatch, tmp_path):
    """A one-second cached cue needs 25 round trips, not the old 100."""
    from hal.drivers.voice.tts import service as tts_module

    writes, references = [], []
    service = _playback_service(tmp_path, writes, [])
    service._device_rate = service._stream_rate = 44100
    monkeypatch.setattr(tts_module.aec, "reference_write", lambda data, rate: references.append(len(data)))
    service._play_wav_inline(_wav(tmp_path, seconds=1.01, rate=44100))

    assert sum(writes) == 44541
    assert writes == [1764] * 25 + [441]
    assert references == writes


def test_cached_cue_stop_discards_audio_after_current_slice(monkeypatch, tmp_path):
    from hal.drivers.voice.tts import service as tts_module

    writes = []
    service = _playback_service(tmp_path, writes, [])
    service._device_rate = service._stream_rate = 44100
    # Model a stop arriving while the current device write is in flight.
    monkeypatch.setattr(tts_module.aec, "reference_write", lambda data, rate: service._stop_event.set())
    service._play_wav_inline(_wav(tmp_path, seconds=1.0, rate=44100))

    assert writes == [1764]


def _history_service(tmp_path):
    service = _playback_service(tmp_path, [], [])
    service._native_mode = False
    service._realtime_feedback = True
    service._last_spoken_text = "The airplane is black."
    service._begin_playback("run:reply")
    return service


def test_history_zero_write_cancel_is_not_spoken(tmp_path):
    service = _history_service(tmp_path)
    service._stop_event.set()
    assert service.history_completion() == ("The airplane is black.", False, False)


def test_history_real_write_and_partial_cancel(tmp_path):
    service = _history_service(tmp_path)
    service._play_wav_inline(_wav(tmp_path))
    assert service.history_completion() == ("The airplane is black.", True, False)
    service._stop_event.set()
    assert service.history_completion() == ("The airplane is black.", False, True)


def test_history_cue_only_is_not_speech(tmp_path):
    service = _history_service(tmp_path)
    _tapped(service, []).write(np.zeros((20, 1), dtype=np.float32), track_playback=False)
    assert service.history_completion() == ("The airplane is black.", False, False)


def test_history_snapshot_survives_new_owner_and_does_not_reuse_old_audio(tmp_path):
    service = _history_service(tmp_path)
    service._note_audio_written()
    previous = service.history_completion()
    service._begin_playback("run:new")
    service._last_spoken_text = "The house is blue."
    assert previous == ("The airplane is black.", True, False)
    assert service.history_completion() == ("The house is blue.", False, False)


def test_history_excludes_native_and_non_feedback_speech(tmp_path):
    service = _history_service(tmp_path)
    service._native_mode = True
    assert service.history_completion() is None
    service._native_mode = False
    service._realtime_feedback = False
    assert service.history_completion() is None


def test_busy_rejected_reply_does_not_replace_active_history(tmp_path):
    service = _history_service(tmp_path)
    service._backend = SimpleNamespace(available=True)
    service._speaker_muted = lambda: False
    service._lock = threading.Lock()
    service._lock.acquire()
    service._interruptible = False
    service._note_audio_written()
    try:
        assert not service.speak("Rejected new reply", realtime_feedback=True, harness_result=True)
        assert service.history_completion() == ("The airplane is black.", True, False)
    finally:
        service._lock.release()
