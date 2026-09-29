"""Audio conversion utilities for PCM16 codec."""

import base64
from math import gcd

import numpy as np
import numpy.typing as npt


def float32_to_base64_pcm16(audio: npt.NDArray[np.float32]) -> str:
    """Convert float32 audio [-1.0, 1.0] to base64-encoded PCM16. Used by OpenAI."""
    clipped = np.clip(audio, -1.0, 1.0)
    pcm16 = (clipped * 32767).astype(np.int16)
    return base64.b64encode(pcm16.tobytes()).decode()


def base64_pcm16_to_float32(b64_audio: str) -> npt.NDArray[np.float32]:
    """Convert base64-encoded PCM16 to float32 [-1.0, 1.0]. Used by OpenAI."""
    return pcm16_bytes_to_float32(base64.b64decode(b64_audio))


def float32_to_pcm16_bytes(audio: npt.NDArray[np.float32]) -> bytes:
    """Convert float32 audio [-1.0, 1.0] to raw PCM16 bytes. Used by Gemini."""
    clipped = np.clip(audio, -1.0, 1.0)
    pcm16 = (clipped * 32767).astype(np.int16)
    return pcm16.tobytes()


def pcm16_bytes_to_float32(data: bytes) -> npt.NDArray[np.float32]:
    """Convert raw PCM16 bytes to float32 [-1.0, 1.0]; drops a trailing odd byte from a split chunk."""
    usable = len(data) - (len(data) % 2)
    if usable <= 0:
        return np.empty(0, dtype=np.float32)
    pcm16 = np.frombuffer(data[:usable], dtype=np.int16)
    return (pcm16.astype(np.float32) / 32767.0)


def resample_float32(
    audio: npt.NDArray[np.float32], src_rate: int, dst_rate: int,
) -> npt.NDArray[np.float32]:
    """Resample float32 audio from src_rate to dst_rate. No-op if rates match."""
    if src_rate == dst_rate:
        return audio
    import scipy.signal
    g = gcd(dst_rate, src_rate)
    return scipy.signal.resample_poly(audio, dst_rate // g, src_rate // g).astype(np.float32)


class StreamingResampler:
    """Frame-by-frame overlap-save resampler; delays the stream by `history` input samples.

    Resampling isolated 20 ms frames rings at every boundary (a 50 Hz click train).
    """

    def __init__(self, src_rate: int, dst_rate: int, history: int = 64) -> None:
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self._history = history if src_rate != dst_rate else 0
        # [history][not-yet-emitted lookahead] — starts as silence history.
        self._buf: npt.NDArray[np.float32] = np.zeros(self._history, dtype=np.float32)

    def process(self, audio: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
        if self.src_rate == self.dst_rate or len(audio) == 0:
            return audio
        h = self._history
        buf = np.concatenate([self._buf, audio])
        end_in = len(buf) - h  # input index up to which output is reliable
        if end_in <= h:
            self._buf = buf
            return np.zeros(0, dtype=np.float32)
        out = resample_float32(buf, self.src_rate, self.dst_rate)
        lo = h * self.dst_rate // self.src_rate
        hi = end_in * self.dst_rate // self.src_rate
        # Keep h samples before the cut (history) and the h after it (lookahead
        # whose output could not be trusted yet) for the next call.
        self._buf = buf[end_in - h:]
        return out[lo:hi]

    def reset(self) -> None:
        self._buf = np.zeros(self._history, dtype=np.float32)
