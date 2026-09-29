"""High-pass filter to remove DC offset and low-frequency rumble."""

import numpy as np
from scipy.signal import butter, sosfiltfilt

from .base import Audio, AudioProcessorBase

DEFAULT_CUTOFF_HZ: float = 80.0
DEFAULT_ORDER: int = 4


class HighPassFilter(AudioProcessorBase):
    """4th-order Butterworth high-pass filter (zero-phase)."""

    def __init__(self, cutoff_hz: float = DEFAULT_CUTOFF_HZ, order: int = DEFAULT_ORDER) -> None:
        super().__init__()
        self._cutoff_hz: float = cutoff_hz
        self._order: int = order

    def _process_impl(self, input: Audio) -> Audio:
        if input.waveform.shape[0] == 0:
            return input

        nyq: float = 0.5 * float(input.sample_rate)
        if self._cutoff_hz <= 0.0 or self._cutoff_hz >= nyq:
            return input

        try:
            sos = butter(self._order, self._cutoff_hz / nyq, btype="highpass", output="sos")
            filtered = sosfiltfilt(sos, input.waveform)
            return Audio(
                waveform=np.asarray(filtered, dtype=np.float32),
                sample_rate=input.sample_rate,
            )
        except Exception as exc:
            self._logger.warning("HighPassFilter failed, passing through: %s", exc)
            return input
