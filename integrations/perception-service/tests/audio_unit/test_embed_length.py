"""Unit tests for the speaker-embedder frame cap (no model, no GPU; #555)."""

from pathlib import Path

import numpy as np
import pytest

from core.models.media import Audio
from core.perception.audio.predictors.base import AudioEmbedder

SR = 16_000
CACHED_ENGINE_MAX_FRAMES = 5546  # profile max T on prod (issue #555)


class _FakeSession:
    """Records every ONNX input shape; returns one 8-dim vector per batch row."""

    def __init__(self) -> None:
        self.shapes: list[tuple[int, ...]] = []

    def run(self, _outputs, feeds):
        x = next(iter(feeds.values()))
        self.shapes.append(tuple(x.shape))
        return [np.ones((x.shape[0], 8), dtype=np.float32)]


def _embedder(batch_size: int = 4) -> AudioEmbedder:
    emb = AudioEmbedder(model_path=Path("unused.onnx"), batch_size=batch_size)
    emb._session = _FakeSession()
    return emb


def _audio_frames(frames: int) -> Audio:
    """Audio that yields exactly ``frames`` fbank frames (25 ms window, 10 ms shift)."""
    n = 400 + (frames - 1) * 160
    rng = np.random.default_rng(0)
    return Audio(waveform=(rng.standard_normal(n) * 0.1).astype(np.float32), sample_rate=SR)


def test_max_frames_is_inside_cached_engine_profile():
    assert AudioEmbedder.MAX_FRAMES == 3000
    assert AudioEmbedder.MAX_FRAMES < CACHED_ENGINE_MAX_FRAMES


@pytest.mark.parametrize("frames,expected", [(2000, 2000), (3000, 3000), (3001, 3000), (5546, 3000)])
def test_whole_utterance_is_cropped_to_max_frames(frames, expected):
    emb = _embedder()
    emb._predict_impl([_audio_frames(frames)], preprocess=False, use_sliding_window=False)
    assert emb._session.shapes == [(1, expected, 80)]


def test_each_audio_is_cropped_independently():
    emb = _embedder()
    emb._predict_impl(
        [_audio_frames(4000), _audio_frames(900)], preprocess=False, use_sliding_window=False,
    )
    assert emb._session.shapes == [(1, 3000, 80), (1, 900, 80)]


def test_sliding_long_clip_uses_fixed_windows_within_batch_size():
    emb = _embedder(batch_size=4)
    out = emb._predict_impl([_audio_frames(6000)], preprocess=False, use_sliding_window=True)
    assert all(t == 600 and b <= 4 for b, t, _ in emb._session.shapes)
    # 3000 frames, window 600, hop 400 -> starts 0..2400 -> 7 windows.
    assert sum(b for b, _, _ in emb._session.shapes) == 7
    assert out[0].chunk_embeddings.shape == (7, 8)
