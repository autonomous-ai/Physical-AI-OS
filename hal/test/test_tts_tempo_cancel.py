"""Cancellation must release the filter and never revive an old producer."""
import queue
import shutil
import subprocess
import threading

import httpx
import pytest

from hal.drivers.voice.tts.elevenlabs import ElevenLabsTTSBackend
from hal.drivers.voice.tts.service import TTSService


@pytest.mark.parametrize("timeout", [False, True])
def test_cancel_pending_http_reaps_filter_and_next_request_works(monkeypatch, timeout):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    entered, release, cancelled, closed = (threading.Event() for _ in range(4))
    processes, feeders, output, errors = [], [], [], []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield b"\x01\x00" * 24000

        def close(self):
            closed.set()

    def respond(request):
        if not entered.is_set():
            feeders.append(threading.current_thread())
            entered.set()
            assert release.wait(5)
            if timeout:
                raise httpx.ReadTimeout("simulated stalled headers")
        return httpx.Response(200, stream=Body())

    monkeypatch.setattr("hal.drivers.voice.tts.tempo.subprocess.Popen", spawn)
    backend = object.__new__(ElevenLabsTTSBackend)
    backend._api_key = "test"
    backend._base_url = "https://test.example"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        backend._client = client

        def consume():
            try:
                output.extend(backend.stream_pcm("Old", "Rachel", "eleven_v3", 1.2,
                                                 cancelled=cancelled.is_set))
            except Exception as exc:
                errors.append(exc)

        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        try:
            assert entered.wait(2)
            cancelled.set()
            consumer.join(2)
            assert not consumer.is_alive(), "cancel blocked on HTTP"
            assert processes[0].poll() is not None
            assert not output and not errors
            # A new utterance can run even before the old HTTP call returns.
            assert b"".join(backend.stream_pcm("New", "Rachel", "eleven_v3", 1.2))
        finally:
            release.set()
            consumer.join(2)
            for feeder in feeders:
                feeder.join(2)
        assert all(not feeder.is_alive() for feeder in feeders)
        assert all(process.poll() is not None for process in processes)
        assert not output and not errors
        assert closed.is_set()


@pytest.mark.parametrize("tail", [False, True])
def test_cancel_full_producer_queue_survives_stop_event_reset(monkeypatch, tail):
    from hal.drivers.voice.tts import service
    monkeypatch.setattr(service.hal_config, "LIVE_MODE", False)
    svc = object.__new__(TTSService)
    svc._speaking = True
    svc._stop_event = threading.Event()
    svc._pending_queue = []
    svc._pending_queue_lock = threading.Lock()
    svc._drain_queues = []
    svc._drain_queues_lock = threading.Lock()
    svc._max_retries = 0
    svc._speed = 1.2
    blocked = threading.Event()
    frames = []

    class FullQueue(queue.Queue):
        def put(self, item, block=True, timeout=None):
            if block:
                blocked.set()
            return super().put(item, block, timeout)

    def samples(*args, **kwargs):
        for i in range(3):
            frames.append(i)
            yield i

    svc._iter_tts_samples = samples
    out = FullQueue(maxsize=1)
    out.put_nowait("occupied")
    target = svc._tail_producer if tail else svc._head_producer
    args = (["old", "never"], 24000, out) if tail else ("old", 24000, out, (1, 1))
    worker = threading.Thread(target=target, args=args, daemon=True)
    worker.start()
    assert blocked.wait(2)
    svc.stop()
    svc._stop_event.clear()  # A new turn claims playback immediately.
    worker.join(2)
    assert not worker.is_alive(), "old producer remained blocked on its abandoned queue"
    assert out.get_nowait() == "occupied"
    assert len(frames) <= 2
