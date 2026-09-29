"""Abstract base class for face detectors."""

from abc import ABC

import cv2.typing as cv2t

from core.models.face import FaceCrop, RawFaceDetection
from core.perception.base import PredictorBase


class FaceDetector(PredictorBase[cv2t.MatLike, RawFaceDetection], ABC):
    """Base interface for face detectors."""

    def extract_crops(
        self,
        input: list[cv2t.MatLike],
    ) -> list[list[FaceCrop]]:
        """Detect faces and return crops with metadata per frame."""
        detections: list[RawFaceDetection] = self.predict(input)
        return self.extract_crops_from_raw(input, detections)

    @staticmethod
    def extract_crops_from_raw(
        frames: list[cv2t.MatLike],
        detections: list[RawFaceDetection],
    ) -> list[list[FaceCrop]]:
        """Convert raw detections to face crops without re-running predict."""
        results: list[list[FaceCrop]] = []
        for i, raw in enumerate(detections):
            frame: cv2t.MatLike = frames[i]
            H, W = frame.shape[:2]
            crops: list[FaceCrop] = []

            if len(raw.bbox_xyxy) == 0:
                results.append(crops)
                continue

            for j in range(len(raw.bbox_xyxy)):
                x1, y1, x2, y2 = raw.bbox_xyxy[j]
                x1, y1 = int(max(0, x1)), int(max(0, y1))
                x2, y2 = int(min(W, x2)), int(min(H, y2))

                if x1 >= x2 or y1 >= y2:
                    continue

                crop: cv2t.MatLike = frame[y1:y2, x1:x2]
                crops.append(
                    FaceCrop(
                        crop=crop,
                        bbox_xyxy=[int(x1), int(y1), int(x2), int(y2)],
                        confidence=float(raw.confidence[j]),
                    )
                )

            results.append(crops)

        return results
