"""Frame downscale + bbox coordinate mapping."""

from typing import Tuple

import cv2
import numpy as np
import numpy.typing as npt

from hal.drivers.tracking import constants as C


def downscale(frame: npt.NDArray[np.uint8]) -> Tuple[npt.NDArray[np.uint8], float]:
    """Return (small_frame, scale) with scale = small_w / orig_w (≤ 1.0)."""
    if not C.VISION_MAX_WIDTH:
        return frame, 1.0
    h, w = frame.shape[:2]
    if w <= C.VISION_MAX_WIDTH:
        return frame, 1.0
    scale = C.VISION_MAX_WIDTH / float(w)
    small = cv2.resize(frame, (C.VISION_MAX_WIDTH, max(1, int(round(h * scale)))),
                       interpolation=cv2.INTER_AREA)
    return small, scale


def scale_bbox(bbox: Tuple[int, int, int, int], factor: float) -> Tuple[int, int, int, int]:
    """Scale a bbox by `factor` (use scale to go orig→small, 1/scale for small→orig)."""
    if factor == 1.0:
        return tuple(int(v) for v in bbox)
    return (int(round(bbox[0] * factor)), int(round(bbox[1] * factor)),
            max(1, int(round(bbox[2] * factor))), max(1, int(round(bbox[3] * factor))))
