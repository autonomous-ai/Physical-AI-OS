"""Abstract base class for person detectors."""

import logging
from abc import ABC

import cv2
import cv2.typing as cv2t
import numpy as np

from core.models.person import RawPersonDetection
from core.perception.base import PredictorBase

logger = logging.getLogger(__name__)


class PersonDetector(PredictorBase[cv2t.MatLike, RawPersonDetection], ABC):
    """Base interface for person detectors."""

    def extract_largest_crop(
        self,
        input: list[cv2t.MatLike],
        min_area_ratio: float = 0.0,
    ) -> list[cv2.typing.MatLike | None]:
        """Crop of the largest person per frame with area >= ``min_area_ratio``, else None."""
        detections: list[RawPersonDetection] = self.predict(input)
        return self.extract_largest_crop_from_raw(input, detections, min_area_ratio)

    @staticmethod
    def extract_largest_crop_from_raw(
        frames: list[cv2t.MatLike],
        detections: list[RawPersonDetection],
        min_area_ratio: float = 0.0,
    ) -> list[cv2.typing.MatLike | None]:
        """Convert raw detections to largest-person crops without re-running predict."""
        cropped_input: list[cv2.typing.MatLike | None] = []
        for i, detected_people in enumerate(detections):
            if len(detected_people.bbox_xyxy) == 0:
                cropped_input.append(None)
                continue

            H, W = frames[i].shape[:2]
            frame_area: float = float(H * W)

            # bbox_xyxy is [0, 1] — compute pixel area for filtering
            pixel_xyxy = detected_people.bbox_xyxy.copy()
            pixel_xyxy[:, [0, 2]] *= W
            pixel_xyxy[:, [1, 3]] *= H
            pixel_area = (pixel_xyxy[:, 2] - pixel_xyxy[:, 0]) * (pixel_xyxy[:, 3] - pixel_xyxy[:, 1])

            filter_mask = (pixel_area / frame_area) > min_area_ratio

            if filter_mask.sum() == 0:
                cropped_input.append(None)
                continue

            filtered_area = np.where(filter_mask, pixel_area, 0.0)
            largest_id: int = int(filtered_area.argmax(0))

            x1, y1, x2, y2 = pixel_xyxy[largest_id]

            x1, y1 = int(max(0, x1)), int(max(0, y1))
            x2, y2 = int(min(W, x2)), int(min(H, y2))

            if x1 >= x2 or y1 >= y2:
                cropped_input.append(None)
                continue

            cropped_input.append(frames[i][y1:y2, x1:x2])

        return cropped_input
