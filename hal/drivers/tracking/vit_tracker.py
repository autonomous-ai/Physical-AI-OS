"""OpenCV single-object tracker backend (ViT preferred)."""

import logging
import os
from typing import Optional, Tuple

import cv2
import numpy as np
import numpy.typing as npt

from hal.drivers.tracking.frame_utils import downscale, scale_bbox

logger = logging.getLogger(__name__)

VIT_MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", "vittrack.onnx")


def create_tracker():
    """Create best available OpenCV tracker."""
    def _make_vit():
        params = cv2.TrackerVit_Params()
        params.net = VIT_MODEL_PATH
        return cv2.TrackerVit.create(params)

    # ViT first — has getTrackingScore() for ghost-lock detection.
    candidates = [
        ("ViT",  _make_vit),
        ("CSRT", lambda: cv2.TrackerCSRT.create()),
        ("KCF",  lambda: cv2.TrackerKCF.create()),
        ("MIL",  lambda: cv2.TrackerMIL.create()),
    ]
    for name, factory in candidates:
        try:
            tracker = factory()
            has_score = hasattr(tracker, "getTrackingScore")
            logger.info("Using OpenCV tracker: %s (confidence=%s)", name, "yes" if has_score else "no")
            return tracker
        except (AttributeError, cv2.error, Exception):
            continue
    return None


def get_tracking_score(tracker) -> float:
    """ViT confidence score; other trackers return 1.0 (no signal)."""
    try:
        return float(tracker.getTrackingScore())
    except (AttributeError, Exception):
        return 1.0


def vit_init(tracker, frame: npt.NDArray[np.uint8],
             bbox_orig: Tuple[int, int, int, int]) -> Optional[bool]:
    """Init `tracker` on the downscaled frame with the bbox scaled to match."""
    small, scale = downscale(frame)
    return tracker.init(small, scale_bbox(bbox_orig, scale))


def vit_update(tracker, frame: npt.NDArray[np.uint8]):
    """Update `tracker` on the downscaled frame; return (ok, bbox_orig) with
    the bbox mapped back to original camera coords."""
    small, scale = downscale(frame)
    ok, bbox_s = tracker.update(small)
    if ok:
        return ok, scale_bbox(bbox_s, 1.0 / scale if scale else 1.0)
    return ok, bbox_s
