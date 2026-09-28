"""Harness result audio identifies its source without changing voice or speech metrics."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from fastapi import HTTPException

from hal.drivers.voice.tts.service import TTSService
from hal.models import SpeakRequest
from hal.routes import voice


def service():
    svc = TTSService.__new__(TTSService)
    svc._np = np
    svc._sd = Mock()
    svc._backend = SimpleNamespace(available=True, volume_boost=1.0)
    svc._device_rate = 24000
    svc._stop_event = threading.Event()
    svc._speaker_muted = lambda: False
    return svc


def test_result_chord_is_short_and_distinct_from_capture():
    svc = service()
    samples = svc._harness_result_chime_samples(24000)
    assert samples.shape == (4800, 1)
    assert samples.dtype == np.float32
    assert np.max(abs(samples)) <= .28
    for finished in (False, True):
        assert len(samples) != len(svc._harness_capture_chime_samples(24000, finished=finished))


@pytest.mark.parametrize('reason', ['muted', 'cancelled', 'cancel_during'])
def test_result_cue_obeys_utterance_cancellation_and_mute(reason):
    svc = service()
    stream = Mock()
    if reason == 'muted':
        svc._speaker_muted = lambda: True
    elif reason == 'cancelled':
        svc._stop_event.set()
    else:
        stream.write.side_effect = lambda *args, **kwargs: svc._stop_event.set()
    assert not svc._write_harness_result_chime(stream, 24000)
    assert stream.write.call_count == (1 if reason == 'cancel_during' else 0)


@pytest.mark.parametrize('flag', [False, True])
def test_route_only_forwards_explicit_result_flag(monkeypatch, flag):
    svc = SimpleNamespace(available=True, speak=Mock(return_value=True))
    monkeypatch.setattr(voice.state, 'tts_service', svc)
    monkeypatch.setattr(voice.state, '_speaker_muted', False)
    monkeypatch.setattr(voice.state, 'music_service', None)
    assert voice.speak_text(SpeakRequest(text='Done', harness_result=flag)) == {'status': 'ok'}
    assert svc.speak.call_args.kwargs.get('harness_result', False) == flag
    if not flag:
        assert 'harness_result' not in svc.speak.call_args.kwargs


@pytest.mark.parametrize('reason', ['muted', 'music', 'cached', 'prerender'])
def test_rejected_route_never_admits_result_audio(monkeypatch, reason):
    svc = SimpleNamespace(available=True, speak=Mock(return_value=True))
    monkeypatch.setattr(voice.state, 'tts_service', svc)
    monkeypatch.setattr(voice.state, '_speaker_muted', reason == 'muted')
    monkeypatch.setattr(voice.state, 'music_service', SimpleNamespace(streaming=True) if reason == 'music' else None)
    req = SpeakRequest(text='Done', harness_result=True, cached=reason == 'cached', prerender=reason == 'prerender')
    if reason == 'muted':
        assert voice.speak_text(req) == {'status': 'suppressed'}
    else:
        with pytest.raises(HTTPException):
            voice.speak_text(req)
    svc.speak.assert_not_called()


@pytest.mark.parametrize('flag,empty,cancelled,retry,tail_only', [
    (True, False, False, False, False), (False, False, False, False, False),
    (True, True, False, False, False), (True, False, True, False, False),
    (True, False, False, True, False), (True, False, False, False, True),
])
def test_worker_cue_precedes_speech_once_without_marking_speech_start(flag, empty, cancelled, retry, tail_only):
    svc = service()
    svc._stream_lock = threading.Lock()
    events = []
    attempts = [0]

    def write(data, *, track_playback=True):
        events.append('speech' if track_playback else 'cue')
        if retry and track_playback and attempts[0] == 0:
            attempts[0] += 1
            raise RuntimeError('stream failed')

    svc._ensure_stream = lambda rate: SimpleNamespace(write=write)
    svc._split_text_into_growing_sentence_chunks = lambda text: [text, 'tail'] if tail_only else [text]
    svc._forget_drain_queues = lambda: None
    svc._register_drain_queue = lambda q: None
    svc._drain_pending_queue = lambda stream: 0
    svc._note_playback_done = lambda: None
    svc._release_or_drain_live_queue = lambda: None
    svc._on_speak_end = None
    svc._on_speak_start = lambda: events.append('speech_start')
    svc._invalidate_stream = lambda: None
    svc._probe_device_rate = lambda **kwargs: None

    def produce(text, rate, q, position):
        if not empty and not tail_only:
            q.put(np.ones((24, 1), dtype=np.float32))
        q.put(None)

    svc._head_producer = produce

    def produce_tail(chunks, rate, q):
        q.put(np.ones((24, 1), dtype=np.float32))
        q.put(None)

    svc._tail_producer = produce_tail
    if cancelled:
        svc._stop_event.set()
    svc._speak_sync('Done', harness_result=flag)
    if empty or cancelled:
        assert events == []
    elif flag:
        assert events[:20] == ['cue'] * 20
        if tail_only:
            assert events[20:] == ['speech']
        else:
            assert events[20:22] == ['speech_start', 'speech']
        assert events.count('cue') == 20
    else:
        assert events == ['speech_start', 'speech']


def test_result_bypasses_cache_and_busy_never_starts_worker(monkeypatch):
    svc = service()
    svc._optional_speech_blocked = lambda *args: False
    svc._tts_cache_path = lambda text: pytest.fail('result looked up speech-only cache')
    svc._lock = threading.Lock()
    svc._claim_speech = lambda *args: True
    svc._interruptible = False
    worker = Mock()
    monkeypatch.setattr('hal.drivers.voice.tts.service.threading.Thread', worker)
    assert svc.speak('Done', harness_result=True)
    assert worker.call_args.kwargs['kwargs'] == {'harness_result': True}
    worker.reset_mock()
    assert not svc.speak('Done', harness_result=True)
    worker.assert_not_called()
