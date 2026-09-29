"""Continuous linear PCM resampling with state owned by one synthesis request."""

import numpy as np


class PCMResampler:
    """Keep the sample clock and boundary sample across network chunks."""

    def __init__(self, src_rate: int, dst_rate: int):
        self.src_rate = src_rate
        self.dst_rate = dst_rate
        self._received = 0
        self._emitted = 0
        self._tail = np.empty(0, dtype=np.float32)

    def process(self, samples, *, final: bool = False):
        samples = np.asarray(samples, dtype=np.float32)
        if self.src_rate == self.dst_rate:
            return samples
        start = self._received - len(self._tail)
        buf = np.concatenate((self._tail, samples))
        self._received += len(samples)
        if not len(buf):
            return np.empty(0, dtype=np.float32)
        if final:
            end = (self._received * self.dst_rate + self.src_rate - 1) // self.src_rate
        else:
            end = ((self._received - 1) * self.dst_rate) // self.src_rate + 1
        positions = (
            np.arange(self._emitted, end, dtype=np.int64) * self.src_rate
        ) / self.dst_rate - start
        out = np.interp(positions, np.arange(len(buf)), buf).astype(np.float32)
        self._emitted = end
        keep = min(max(self._emitted * self.src_rate // self.dst_rate - start, 0), len(buf) - 1)
        self._tail = buf[keep:].copy()
        return out
