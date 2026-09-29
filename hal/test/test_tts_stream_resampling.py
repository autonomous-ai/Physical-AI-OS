"""Provider PCM must sound the same regardless of network packet boundaries."""

import math
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from hal.drivers.voice.tts.service import TTSService


def _service(packets, src_rate=24000, boost=1.0):
    svc = object.__new__(TTSService)
    svc._np = np
    svc._stop_event = threading.Event()
    svc._voice = "test"
    svc._model = "test"
    svc._speed = 1.1
    svc._instructions = None
    svc._backend = SimpleNamespace(
        sample_rate=src_rate, volume_boost=boost,
        stream_pcm=lambda **kwargs: iter(packets),
    )
    return svc


def _collect(frames):
    frames = list(frames)
    assert all(f.dtype == np.float32 and f.ndim == 2 and f.shape[1] == 1 for f in frames)
    return np.concatenate(frames).ravel() if frames else np.empty(0, dtype=np.float32)


@pytest.mark.parametrize("src,dst", [(24000, 44100), (24000, 48000), (22050, 44100),
                                      (48000, 16000), (24000, 24000)])
@pytest.mark.parametrize("packet_bytes", [1, 7, 4096, 100000])
def test_pcm_matches_continuous_sample_clock_for_any_packet_boundaries(src, dst, packet_bytes):
    pcm = (np.sin(2 * np.pi * 3100 * np.arange(2401) / src) * 24000).astype(np.int16)
    raw = pcm.tobytes()
    packets = [raw[i:i + packet_bytes] for i in range(0, len(raw), packet_bytes)]
    actual = _collect(_service(packets, src)._iter_tts_samples("hello", dst))
    count = math.ceil(len(pcm) * dst / src)
    expected = np.interp(np.arange(count) * src / dst, np.arange(len(pcm)), pcm / 32768.)
    assert len(actual) == count
    np.testing.assert_allclose(actual, expected, atol=1e-7, rtol=0)


@pytest.mark.parametrize("raw", [b"", b"\x01", b"\x00\x40", b"\x00\x40\xff"])
def test_empty_short_and_incomplete_pcm_at_eof(raw):
    actual = _collect(_service([raw])._iter_tts_samples("hello", 44100))
    expected = [0.5, 0.5] if len(raw) >= 2 else []
    np.testing.assert_array_equal(actual, expected)


def test_cancel_does_not_flush_held_sample():
    svc = _service([b"\x00\x40"])
    frames = svc._iter_tts_samples("hello", 44100)
    np.testing.assert_array_equal(next(frames), [[0.5]])
    svc._stop_event.set()
    assert list(frames) == []


def test_parallel_syntheses_do_not_share_resampling_state():
    raw = np.arange(103, dtype=np.int16).tobytes()
    svc = _service([raw[:77], raw[77:]])
    head = svc._iter_tts_samples("head", 44100)
    tail = svc._iter_tts_samples("tail", 44100)
    first_head = next(head)
    actual_tail = _collect(tail)
    actual_head = _collect([first_head, *head])
    np.testing.assert_array_equal(actual_head, actual_tail)
    np.testing.assert_array_equal(actual_head, _collect(svc._iter_tts_samples("next", 44100)))


def test_gain_also_applies_to_flushed_tail():
    actual = _collect(_service([b"\x00\x20"], boost=2.5)._iter_tts_samples("hello", 44100))
    np.testing.assert_array_equal(actual, [0.625, 0.625])
