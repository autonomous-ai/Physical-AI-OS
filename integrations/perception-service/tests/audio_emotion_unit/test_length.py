"""Unit tests for the SER input-length bound (no model, no GPU)."""

from pathlib import Path

import numpy as np

from core.perception.audio_emotion.length import fit_length
from core.perception.audio_emotion.predictors.emotion2vec import Emotion2VecPlusLargeRecognizer

MIN = 32_000
MAX = 128_000


def test_fit_length_keeps_in_range_clip_unchanged():
    w = np.arange(80_000, dtype=np.float32)
    out = fit_length(w, MIN, MAX)
    assert out.shape == (80_000,)
    np.testing.assert_array_equal(out, w)


def test_fit_length_crops_long_clip_to_last_max():
    w = np.arange(480_000, dtype=np.float32)  # 30 s
    out = fit_length(w, MIN, MAX)
    assert out.shape == (MAX,)
    np.testing.assert_array_equal(out, w[-MAX:])


def test_fit_length_pads_short_clip():
    w = np.ones(16_000, dtype=np.float32)  # 1 s
    out = fit_length(w, MIN, MAX)
    assert out.shape == (MIN,)
    assert out.dtype == np.float32
    np.testing.assert_array_equal(out[:16_000], w)
    assert not out[16_000:].any()


def test_fit_length_pads_empty_clip():
    out = fit_length(np.zeros(0, dtype=np.float32), MIN, MAX)
    assert out.shape == (MIN,)
    assert not out.any()


def test_fit_length_boundaries_unchanged():
    for n in (MIN, MAX):
        assert fit_length(np.ones(n, dtype=np.float32), MIN, MAX).shape == (n,)


def test_recognizer_bounds_match_constants():
    rec = Emotion2VecPlusLargeRecognizer(model_path=Path("unused.onnx"))
    assert rec.min_samples == MIN
    assert rec.max_samples == MAX


def test_recognizer_clamps_batch_size_to_one():
    rec = Emotion2VecPlusLargeRecognizer(model_path=Path("unused.onnx"), batch_size=4)
    assert rec._batch_size == 1
