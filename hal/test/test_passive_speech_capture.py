"""Passive agent speech must not steal an automatic microphone capture."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from hal.drivers.voice.tts import service as module
from hal.drivers.voice.tts.device_input_gate import PassiveSpeechSuppressed
from hal.drivers.voice.tts.service import _PendingSpeech
from hal.test.test_tts_device_input_gate import service


@pytest.fixture
def tts(tmp_path, monkeypatch):
    tts = service(tmp_path)
    tts._on_unspoken_reply = Mock()
    tts.stop = Mock()
    workers = []

    class Worker:
        def __init__(self, target, args=(), **kwargs):
            self.target = target

        def start(self):
            workers.append(self.target)

    monkeypatch.setattr(module, 'threading', SimpleNamespace(
        Thread=Worker, Event=threading.Event, Lock=threading.Lock,
    ))
    tts.workers = workers
    return tts


@pytest.mark.parametrize('method', ['speak', 'speak_cached', 'speak_queue'])
def test_passive_agent_cannot_claim_active_capture(tts, method):
    token = tts.begin_input_capture()
    with pytest.raises(PassiveSpeechSuppressed):
        getattr(tts, method)('Open a window.', realtime_feedback=True,
                             passive_sensing=True, turn_id='environment')
    assert not tts.speaking and not tts._lock.locked()
    assert not tts.workers
    tts.stop.assert_not_called()
    tts._on_unspoken_reply.assert_called_once_with('Open a window.')
    tts.end_input_capture(token)
    assert getattr(tts, method)('Open a window.', realtime_feedback=True,
                                passive_sensing=True, turn_id='environment')
    assert tts.speaking


def test_capture_suppression_does_not_advance_sequence_or_stop_reply(tts):
    tts._latest_queue_turn_seq = 3
    tts._latest_queue_turn_id = 'voice'
    tts._speaking = True
    tts.begin_input_capture()
    for text in ['The air is stuffy.', 'Open a window.']:
        with pytest.raises(PassiveSpeechSuppressed):
            tts.speak_queue(text, realtime_feedback=True, passive_sensing=True,
                            turn_id='environment', turn_seq=4)
    assert tts._latest_queue_turn_seq == 3
    assert tts._latest_queue_turn_id == 'voice'
    assert not tts._pending_queue
    assert not tts.workers
    tts.stop.assert_not_called()
    assert tts._on_unspoken_reply.call_count == 2


@pytest.mark.parametrize('method', ['speak', 'speak_cached', 'speak_queue'])
def test_user_reply_still_admitted_during_automatic_capture(tts, method):
    tts.begin_input_capture()
    assert getattr(tts, method)('Four.', realtime_feedback=True, turn_id='voice')
    assert tts.speaking
    tts._on_unspoken_reply.assert_not_called()


@pytest.mark.parametrize('method,cached', [('speak', False), ('speak_cached', False),
                                           ('speak_queue', False), ('speak_queue', True)])
def test_capture_starting_during_cache_lookup_blocks_claim_once(tts, method, cached):
    def path(_):
        tts.begin_input_capture()
        return SimpleNamespace(exists=lambda: cached, name='notice')
    tts._tts_cache_path = path
    with pytest.raises(PassiveSpeechSuppressed):
        getattr(tts, method)('Open a window.', realtime_feedback=True,
                             passive_sensing=True, turn_id='environment')
    assert not tts.speaking and not tts._lock.locked()
    assert not tts.workers
    tts.stop.assert_not_called()
    tts._on_unspoken_reply.assert_called_once_with('Open a window.')


def test_streamed_passive_segments_keep_policy_in_pending_queue(tts):
    assert tts.speak_queue('The air is stuffy.', realtime_feedback=True,
                           passive_sensing=True, turn_id='environment', turn_seq=1)
    assert tts.speak_queue('Open a window.', realtime_feedback=True,
                           passive_sensing=True, turn_id='environment', turn_seq=1)
    assert len(tts._pending_queue) == 1
    assert tts._pending_queue[0].passive_sensing
    assert tts._pending_queue[0].realtime_feedback


@pytest.mark.parametrize('capture_timing', ['already_active', 'waiting_for_synthesis'])
def test_queued_passive_segment_rechecks_capture_before_audio(tts, monkeypatch, capture_timing):
    monkeypatch.setattr(module.hal_config, 'LIVE_MODE', False)
    item = _PendingSpeech('Open a window.', False, owner='run:environment',
                           realtime_feedback=True, passive_sensing=True)
    if capture_timing == 'already_active':
        tts.begin_input_capture()
        item.frame_queue = Mock()
    else:
        def first(**_):
            tts.begin_input_capture()
            return np.ones(64, dtype=np.float32)
        item.frame_queue = SimpleNamespace(get=first)
    tts._pending_queue = [item]
    stream = Mock()
    assert tts._drain_pending_queue(stream) == 0
    stream.write.assert_not_called()
    tts._begin_playback.assert_not_called()
    tts._on_unspoken_reply.assert_called_once_with('Open a window.')
    assert item.cancelled.is_set()
    if capture_timing == 'already_active':
        item.frame_queue.get.assert_not_called()


@pytest.mark.parametrize('route,cached', [('speak_text', False), ('speak_text', True),
                                        ('speak_queue_text', False)])
def test_http_reports_deliberate_suppression_not_backend_failure(tts, monkeypatch, route, cached):
    from hal.routes import voice
    from hal.models import SpeakRequest
    monkeypatch.setattr(voice.state, 'tts_service', tts)
    monkeypatch.setattr(voice.state, '_speaker_muted', False)
    monkeypatch.setattr(voice.state, 'music_service', None)
    monkeypatch.setattr(voice.state, 'voice_service', None)
    tts.begin_input_capture()
    req = SpeakRequest(text='Open a window.', realtime_feedback=True,
                       passive_sensing=True, cached=cached, turn_id='environment')
    assert getattr(voice, route)(req) == {'status': 'suppressed_capture'}
    tts._on_unspoken_reply.assert_called_once_with('Open a window.')


@pytest.mark.parametrize('method', ['speak', 'speak_cached', 'speak_queue'])
def test_manual_input_keeps_passive_reply_deferred_until_release(tmp_path, method):
    tts = service(tmp_path)
    played = threading.Event()
    tts._on_unspoken_reply = Mock()

    def play(*args, **kwargs):
        tts._speaking = False
        tts._lock.release()
        played.set()

    tts._speak_sync = play
    tts._cached_play_thread = play
    token = tts.begin_device_input()
    assert token is not None
    assert getattr(tts, method)('Open a window.', realtime_feedback=True,
                                passive_sensing=True, turn_id='environment')
    assert not tts.speaking and not played.is_set()
    tts.end_device_input(token)
    assert played.wait(1)
    tts._on_unspoken_reply.assert_not_called()


def test_capture_starting_before_enqueue_cannot_launch_passive_synthesis(tts, monkeypatch):
    tts._speaking = True
    tts._lock.acquire()
    original_pending = module._PendingSpeech

    def begin_capture_before_enqueue(*args, **kwargs):
        item = original_pending(*args, **kwargs)
        tts.begin_input_capture()
        return item

    monkeypatch.setattr(module, '_PendingSpeech', begin_capture_before_enqueue)
    try:
        with pytest.raises(PassiveSpeechSuppressed):
            tts.speak_queue('Open a window.', passive_sensing=True,
                            realtime_feedback=True)
        assert not tts._pending_queue
        assert not tts.workers
        tts.stop.assert_not_called()
        tts._on_unspoken_reply.assert_called_once_with('Open a window.')
    finally:
        tts._lock.release()
