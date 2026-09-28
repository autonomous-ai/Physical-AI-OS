"""Worker isolation, stale decisions and bounded failure for Smart Turn."""

import threading

import pytest

from hal.drivers.voice._internal import smart_turn


class FakeAnalyzer:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.cleaned = threading.Event()
        self.state = "complete"
        self.error = None
        self.audio = []
        self.threads = []

    def clear(self):
        self.threads.append(threading.current_thread())

    def append_audio(self, pcm, is_speech):
        assert is_speech
        self.audio.append(pcm)

    async def analyze_end_of_turn(self):
        self.started.set()
        assert self.release.wait(3)
        if self.error:
            raise self.error
        return self.state, None

    async def cleanup(self):
        self.cleaned.set()


@pytest.fixture
def detector(monkeypatch):
    analyzer = FakeAnalyzer()
    monkeypatch.setattr(smart_turn, "_create_analyzer", lambda: (analyzer, "complete"))
    detector = smart_turn.SmartTurnDetector()
    yield detector, analyzer
    detector.close()
    analyzer.release.set()
    detector._thread.join(3)
    assert not detector._thread.is_alive()


def finish(detector, analyzer):
    analyzer.release.set()
    # Synchronize through the worker lock, without relying on a fixed sleep.
    import time

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with detector._lock:
            if not detector._busy:
                return
        threading.Event().wait(0.001)
    pytest.fail("Smart Turn worker did not finish")


def test_single_flight_and_capture_thread_isolation(detector):
    detector, analyzer = detector
    assert not detector.available and not detector.failed
    assert detector.submit("first", b"\x00\x01")
    assert analyzer.started.wait(3)
    assert detector.available
    assert not detector.submit("second", b"\x00\x01")
    assert detector.poll("first") is None
    assert analyzer.threads[0] is not threading.current_thread()
    finish(detector, analyzer)
    assert detector.poll("first") is True
    assert detector.poll("first") is None


def test_stale_result_is_discarded_and_next_request_can_run(detector):
    detector, analyzer = detector
    assert detector.submit(1, b"\x00\x01")
    assert analyzer.started.wait(3)
    finish(detector, analyzer)
    assert detector.poll(2) is None
    assert detector.poll(1) is None
    analyzer.state = "incomplete"
    assert detector.submit(2, b"\x00\x01")
    finish(detector, analyzer)
    assert detector.poll(2) is False


def test_snapshot_is_bounded_and_immutable(detector):
    detector, analyzer = detector
    pcm = bytearray(b"\x01\x02" * (16000 * 9))
    assert detector.submit(1, pcm)
    pcm[-2:] = b"\x00\x00"
    assert analyzer.started.wait(3)
    assert len(analyzer.audio[0]) == 8 * 16000 * 2
    assert analyzer.audio[0][-2:] == b"\x01\x02"


def test_invalid_audio_is_not_submitted(detector):
    detector, analyzer = detector
    assert not detector.submit(1, b"")
    assert not detector.submit(1, b"\x00")
    assert not analyzer.started.is_set()


def test_close_does_not_wait_for_inference(detector):
    detector, analyzer = detector
    assert detector.submit(1, b"\x00\x01")
    assert analyzer.started.wait(3)
    detector.close()
    assert not analyzer.release.is_set()
    assert not detector.submit(2, b"\x00\x01")
    assert not detector.available
    finish(detector, analyzer)
    assert detector.poll(1) is None


def test_inference_failure_disables_detector_once(detector, caplog):
    detector, analyzer = detector
    analyzer.error = RuntimeError("broken model")
    assert detector.submit(1, b"\x00\x01")
    assert analyzer.started.wait(3)
    finish(detector, analyzer)
    detector._thread.join(3)
    assert detector.failed and not detector.available
    assert detector.poll(1) is None
    assert not detector.submit(2, b"\x00\x01")
    assert analyzer.cleaned.is_set()
    assert caplog.text.count("Smart Turn unavailable") == 1


def test_missing_package_disables_detector_without_retries(monkeypatch, caplog):
    calls = []

    def missing():
        calls.append(1)
        raise ImportError("pipecat")

    monkeypatch.setattr(smart_turn, "_create_analyzer", missing)
    detector = smart_turn.SmartTurnDetector()
    try:
        assert detector.submit(1, b"\x00\x01")
        detector._thread.join(3)
        assert detector.failed and not detector.available
        assert not detector.submit(2, b"\x00\x01")
        assert calls == [1]
        assert caplog.text.count("Smart Turn unavailable") == 1
    finally:
        detector.close()
