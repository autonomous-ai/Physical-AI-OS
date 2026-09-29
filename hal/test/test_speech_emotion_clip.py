"""select_voiced_span: pick the contiguous <=8 s span with the most voice."""

import numpy as np

from hal.drivers.voice.speech_emotion.emotion2vec import Emotion2VecRecognizer
from hal.drivers.voice.speech_emotion.utils import (
    pcm16_to_wav,
    select_voiced_span,
    wav_to_pcm16,
)

SR = 16_000
MAX_S = 8.0
MAX_N = int(SR * MAX_S)
VOICED_RMS = 2500.0
FRAME_MS = 20


def _clip(total_s: float, bursts: list[tuple[float, float]]) -> np.ndarray:
    """Quiet noise floor (RMS well under VOICED_RMS) with loud sine bursts at [start, end) s."""
    n = int(SR * total_s)
    rng = np.random.default_rng(0)
    out = rng.integers(-150, 151, n).astype(np.int16)
    t = np.arange(n) / SR
    for start, end in bursts:
        mask = (t >= start) & (t < end)
        out[mask] = (10_000 * np.sin(2 * np.pi * 220 * t[mask])).astype(np.int16)
    return out


def _span(samples, out) -> tuple[float, float]:
    """Locate `out` inside `samples` (spans are contiguous slices)."""
    for start in range(0, samples.size - out.size + 1, 320):
        if np.array_equal(samples[start : start + out.size], out):
            return start / SR, (start + out.size) / SR
    raise AssertionError("output is not a contiguous slice of the input")


def _select(samples):
    return select_voiced_span(samples, SR, VOICED_RMS, FRAME_MS, MAX_S)


def test_short_clip_unchanged():
    s = _clip(5.0, [(1.0, 3.0)])
    out = _select(s)
    assert out.size == s.size
    np.testing.assert_array_equal(out, s)


def test_exactly_max_unchanged():
    s = _clip(MAX_S, [(1.0, 3.0)])
    np.testing.assert_array_equal(_select(s), s)


def test_long_clip_crops_to_max_and_contains_burst():
    s = _clip(30.0, [(10.0, 14.0)])
    out = _select(s)
    assert out.size == MAX_N
    start, end = _span(s, out)
    assert start <= 10.0 and end >= 14.0


def test_select_voiced_span_keeps_early_speech():
    s = _clip(30.0, [(0.5, 4.0)])
    start, end = _span(s, _select(s))
    assert start <= 0.5 and end >= 4.0


def test_tie_prefers_latest_span():
    s = _clip(30.0, [(2.0, 4.0), (20.0, 22.0)])
    start, end = _span(s, _select(s))
    assert start <= 20.0 and end >= 22.0
    assert start > 4.0


def test_all_silent_long_clip_returns_last_span():
    s = _clip(30.0, [])
    out = _select(s)
    assert out.size == MAX_N
    np.testing.assert_array_equal(out, s[-MAX_N:])


def _loud_sine_wav(total_s: float) -> bytes:
    """Synthetic 16 kHz mono int16 WAV loud enough to clear both prefilter stages."""
    n = int(SR * total_s)
    t = np.arange(n) / SR
    samples = (10_000 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)
    return pcm16_to_wav(samples, SR)


def _no_network_recognizer(monkeypatch) -> Emotion2VecRecognizer:
    """Recognizer for prefilter()-only tests: no encryption, no Silero."""
    monkeypatch.setattr("hal.config.DL_ENCRYPTION_ENABLED", False)
    monkeypatch.setattr(Emotion2VecRecognizer, "_load_silero", lambda self: None)
    return Emotion2VecRecognizer(url="http://fake-server.invalid", api_key="", timeout_s=1.0)


def test_prefilter_caps_long_passing_clip_to_8s(monkeypatch):
    """A long clip passing the prefilter is uploaded as at most 8 s."""
    rec = _no_network_recognizer(monkeypatch)
    wav_bytes = _loud_sine_wav(20.0)

    out_wav = rec.prefilter(wav_bytes)

    assert out_wav is not None
    samples, sample_rate = wav_to_pcm16(out_wav)
    duration_s = samples.size / sample_rate
    assert 0 < duration_s <= MAX_S
