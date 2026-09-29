"""Input-length bound for the SER model.

Lengths outside the TensorRT shape profile force an engine rebuild mid-request
(issue #492), stalling all GPU routes, so waveforms are clamped to [min, max] samples.
"""

import numpy as np
import numpy.typing as npt


def fit_length(
    waveform: npt.NDArray[np.float32], min_samples: int, max_samples: int
) -> npt.NDArray[np.float32]:
    """Keep the last ``max_samples``; zero-pad anything shorter than ``min_samples``."""
    n = waveform.shape[0]
    if n > max_samples:
        return waveform[n - max_samples :]
    if n < min_samples:
        padded = np.zeros(min_samples, dtype=np.float32)
        padded[:n] = waveform
        return padded
    return waveform
