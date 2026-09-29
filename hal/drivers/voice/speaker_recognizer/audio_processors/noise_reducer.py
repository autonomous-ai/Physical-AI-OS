"""Noise reduction using the noisereduce library."""

import numpy as np

from .base import Audio, AudioProcessorBase

DEFAULT_STATIONARY: bool = False


class NoiseReducer(AudioProcessorBase):
    """Attenuate background noise using non-stationary noise reduction."""

    def __init__(self, stationary: bool = DEFAULT_STATIONARY) -> None:
        super().__init__()
        self._stationary: bool = stationary

    def _process_impl(self, input: Audio) -> Audio:
        if input.waveform.shape[0] == 0:
            return input

        try:
            import noisereduce as nr

            cleaned = nr.reduce_noise(
                y=input.waveform, sr=input.sample_rate, stationary=self._stationary
            )
            return Audio(
                waveform=np.asarray(cleaned, dtype=np.float32),
                sample_rate=input.sample_rate,
            )
        except Exception as exc:
            self._logger.warning("NoiseReducer failed, passing through: %s", exc)
            return input
