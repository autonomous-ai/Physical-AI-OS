"""The live uplink resampler must not ring at frame edges (GPT-Live heard nothing at 24 kHz)."""

import numpy as np

from hal.realtime.utils import StreamingResampler, resample_float32


def _tone(sr=16000, secs=1.0):
    t = np.arange(int(sr * secs)) / sr
    return (0.3 * np.sin(2 * np.pi * 440 * t) + 0.1 * np.sin(2 * np.pi * 1700 * t)).astype(np.float32)


def test_streamed_frames_match_whole_signal_resample_exactly():
    x = _tone()
    whole = resample_float32(x, 16000, 24000)
    rs = StreamingResampler(16000, 24000)
    streamed = np.concatenate([rs.process(x[i:i + 320]) for i in range(0, len(x), 320)])
    n = len(streamed)
    assert n == len(whole) - 64 * 24000 // 16000
    assert np.max(np.abs(whole[:n] - streamed)) == 0.0


def test_same_rate_is_a_passthrough_and_reset_clears_history():
    rs = StreamingResampler(16000, 16000)
    x = np.ones(320, dtype=np.float32)
    assert rs.process(x) is x
    rs = StreamingResampler(16000, 24000)
    rs.process(np.ones(320, dtype=np.float32))
    rs.reset()
    assert not np.any(rs._buf)
