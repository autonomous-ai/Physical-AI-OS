"""YOLO-based person detector for action recognition preprocessing."""

from typing import Any

import cv2.typing as cv2t
import numpy as np
from typing_extensions import override
from ultralytics.models.yolo import YOLO

from core.models.person import RawPersonDetection
from core.perception.person.predictors.base import PersonDetector
from core.utils.common import get_or_default
from core.utils.detection import expand_boxes_xyxy

# COCO class index for "person"
_PERSON_CLASS_ID = 0


class YOLOPersonDetector(PersonDetector):
    """YOLO-based person detector (ultralytics); rate limiting is the caller's job.

    Example: ``YOLOPersonDetector(model_name="yolo12x.pt").start()``
    """

    DEFAULT_MODEL_NAME: str = "yolo12x.pt"
    DEFAULT_CONFIDENCE_THRESHOLD: float = 0.4
    DEFAULT_BBOX_EXPAND_SCALE: float = 2.0

    DEFAULT_MIN_AREA_RATIO: float = 0.25

    def __init__(
        self,
        model_path: str | None = None,
        threshold: float | None = None,
        bbox_expand_scale: float | None = None,
        batch_size: int | None = None,
    ):
        super().__init__(batch_size=batch_size)
        self._model_name: str = get_or_default(model_path, self.DEFAULT_MODEL_NAME)
        self._threshold: float = get_or_default(threshold, self.DEFAULT_CONFIDENCE_THRESHOLD)
        self._bbox_expand_scale: float = get_or_default(
            bbox_expand_scale, self.DEFAULT_BBOX_EXPAND_SCALE
        )

        self._model: YOLO | None = None
        self._running: bool = False

    @override
    def _start_impl(self) -> None:
        """Load the YOLO model weights (blocking)."""
        if self._running:
            self._logger.info("Already running")
            return

        self._model = YOLO(self._model_name)
        self._running = True

    @override
    def _stop_impl(self) -> None:
        self._model = None
        self._running = False

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._model is not None

    @override
    def _predict_impl(
        self, input: list[cv2t.MatLike], *, preprocess: bool = True, **kwargs: Any
    ) -> list[RawPersonDetection]:
        """Run person detection on each frame and return all person detections."""
        _EMPTY: RawPersonDetection = RawPersonDetection(
            bbox_xyxy=np.zeros((0, 4), dtype=np.float32),
            confidence=np.zeros(0, dtype=np.float32),
        )

        person_detections: list[RawPersonDetection] = []
        for frame in input:
            try:
                H, W = frame.shape[:2]
                results = self._model(
                    frame,
                    classes=[_PERSON_CLASS_ID],
                    conf=self._threshold,
                    verbose=False,
                )
                bbox_xyxy_list: list[list[float]] = []
                conf_list: list[float] = []
                for r in results:
                    if r.boxes is None or len(r.boxes) == 0:
                        continue

                    for box in r.boxes:
                        bbox_xyxy_list.append(box.xyxy[0].tolist())
                        conf_list.append(float(box.conf[0]))

                if not bbox_xyxy_list:
                    person_detections.append(_EMPTY)
                else:
                    xyxy = np.array(bbox_xyxy_list, dtype=np.float32)

                    if self._bbox_expand_scale != 1.0:
                        xyxy = expand_boxes_xyxy(xyxy, self._bbox_expand_scale, float(W), float(H))

                    # Normalize to [0, 1]
                    xyxy[:, [0, 2]] /= W
                    xyxy[:, [1, 3]] /= H

                    person_detections.append(
                        RawPersonDetection(
                            bbox_xyxy=xyxy,
                            confidence=np.array(conf_list, dtype=np.float32),
                        )
                    )
            except Exception:
                self._logger.exception("Inference error")
                person_detections.append(_EMPTY)

        return person_detections

    @override
    def preprocess(self, input: list[cv2t.MatLike]) -> list[cv2t.MatLike]:
        """No preprocessing needed — YOLO handles internally."""
        return input
