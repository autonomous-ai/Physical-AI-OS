"""Input-length bound for the SER model.

emotion2vec is served through TensorRT with a fixed shape profile. Any input
length outside that profile would force an engine rebuild inside a live
request (issue #492): it stalls every GPU route for ~30-70 s and ratchets GPU
memory. Every waveform is therefore forced into [min_samples, max_samples]
before it reaches ONNX Runtime.
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
