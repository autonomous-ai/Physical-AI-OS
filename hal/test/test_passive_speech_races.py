"""Passive admission preserves idle behavior without blocking microphone input."""

import queue
import threading
from unittest.mock import Mock

import pytest

from hal.drivers.voice.tts import service as module
from hal.drivers.voice.tts.device_input_gate import PassiveSpeechSuppressed
from hal.drivers.voice.tts.service import _PendingSpeech
from hal.test.test_tts_device_input_gate import service


@pytest.mark.parametrize("method", ["speak", "speak_cached"])
def test_passive_idle_request_still_preempts_interruptible_audio(tmp_path, method):
    tts = service(tmp_path)
    tts._interruptible = True
    tts._speaking = True
    tts._lock.acquire()
    played = threading.Event()

    def stop():
        tts._speaking = False
        tts._lock.release()

    def play(*_args, **_kwargs):
        tts._speaking = False
        tts._lock.release()
        played.set()

    tts.stop = Mock(side_effect=stop)
    tts._speak_sync = play
    tts._cached_play_thread = play
    assert getattr(tts, method)("ambient notice", passive_sensing=True)
    assert played.wait(1)
    tts.stop.assert_called_once()


@pytest.mark.parametrize("method", ["speak", "speak_cached"])
def test_capture_can_start_while_preempted_worker_releases_lock(tmp_path, method):
    tts = service(tmp_path)
    tts._interruptible = True
    tts._speaking = True
    tts._lock.acquire()
    capture_admitted = threading.Event()
    tts._on_unspoken_reply = Mock()
    tts._speak_sync = Mock()
    tts._cached_play_thread = Mock()
    workers = []

    def stop():
        def release_after_capture():
            tts.begin_input_capture()
            capture_admitted.set()
            tts._speaking = False
            tts._lock.release()
        worker = threading.Thread(target=release_after_capture)
        workers.append(worker)
        worker.start()

    tts.stop = Mock(side_effect=stop)
    with pytest.raises(PassiveSpeechSuppressed):
        getattr(tts, method)("ambient notice", passive_sensing=True,
                             realtime_feedback=True)
    for worker in workers:
        worker.join(1)
        assert not worker.is_alive()
    assert capture_admitted.is_set()
    assert not tts._lock.locked()
    tts._speak_sync.assert_not_called()
    tts._cached_play_thread.assert_not_called()
    tts._on_unspoken_reply.assert_called_once_with("ambient notice")


@pytest.mark.parametrize("sequence", [1, 2])
def test_stale_passive_history_callback_does_not_hold_capture_lock(tmp_path, sequence):
    tts = service(tmp_path)
    tts._latest_queue_turn_seq = 2
    tts._latest_queue_turn_id = "newer"
    tts._counter_restarted = lambda *_: False
    capture_admitted = threading.Event()
    callback_allowed_capture = []
    workers = []

    def history(_text):
        def capture():
            token = tts.begin_input_capture()
            capture_admitted.set()
            tts.end_input_capture(token)
        worker = threading.Thread(target=capture)
        workers.append(worker)
        worker.start()
        callback_allowed_capture.append(capture_admitted.wait(1))

    tts._on_unspoken_reply = history
    assert tts.speak_queue("stale", passive_sensing=True, realtime_feedback=True,
                           turn_id="older", turn_seq=sequence)
    for worker in workers:
        worker.join(1)
        assert not worker.is_alive()
    assert callback_allowed_capture == [True]
    assert not tts.speaking


def test_discarded_non_live_pending_producer_exits_when_queue_is_full(tmp_path, monkeypatch):
    monkeypatch.setattr(module.hal_config, "LIVE_MODE", False)
    tts = service(tmp_path)
    tts._device_rate = 24000
    item = _PendingSpeech("ambient", False, passive_sensing=True)
    attempted_write = threading.Event()

    class FullQueue(queue.Queue):
        def put(self, value, *args, **kwargs):
            if value is not None:
                attempted_write.set()
            return super().put(value, *args, **kwargs)

    item.frame_queue = FullQueue(maxsize=1)
    item.frame_queue.put(None)
    tts._split_text_into_growing_sentence_chunks = lambda _: ["ambient"]
    tts._iter_tts_samples = lambda *_args, **_kwargs: iter([object()])
    worker = threading.Thread(target=tts._pre_synth_pending, args=(item,))
    worker.start()
    try:
        assert attempted_write.wait(1)
        tts._discard_passive_pending(item)
        worker.join(2)
        assert not worker.is_alive()
        assert not tts._stop_event.is_set()
    finally:
        tts._stop_event.set()
        worker.join(2)
