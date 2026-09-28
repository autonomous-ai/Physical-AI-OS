"""YOLO person detector using ONNX Runtime (person class only, xyxy output)."""

from pathlib import Path
from typing import Any

import cv2.typing as cv2t
import numpy as np
from typing_extensions import override

from core.models.person import RawPersonDetection
from core.perception.object.predictors.onnx.yolo import YOLOONNXDetector
from core.utils.common import get_or_default
from core.utils.detection import expand_boxes_xyxy, xywh_to_xyxy

from .base import PersonDetector

_PERSON_CLASS_NAME: str = "person"


class YOLOONNXPersonDetector(PersonDetector):
    """Person detection by running YOLO ONNX and filtering to person class."""

    DEFAULT_MODEL_PATH: Path | None = None
    DEFAULT_REMOTE_URL: str | None = None
    DEFAULT_THRESHOLD: float = 0.4
    DEFAULT_IMGSZ: int = 640
    DEFAULT_BBOX_EXPAND_SCALE: float = 2.0

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
        classes_path: Path | None = None,
        threshold: float | None = None,
        imgsz: int | None = None,
        bbox_expand_scale: float | None = None,
        batch_size: int | None = None,
        nms: bool = True,
    ) -> None:
        super().__init__(batch_size=batch_size)
        self._bbox_expand_scale: float = get_or_default(
            bbox_expand_scale, self.DEFAULT_BBOX_EXPAND_SCALE
        )
        self._detector = YOLOONNXDetector(
            model_path=model_path,
            remote_url=remote_url,
            classes_path=classes_path,
            threshold=get_or_default(threshold, self.DEFAULT_THRESHOLD),
            imgsz=get_or_default(imgsz, self.DEFAULT_IMGSZ),
            batch_size=batch_size,
            nms=nms,
        )

    @override
    def _start_impl(self) -> None:
        self._detector.start()

    @override
    def _stop_impl(self) -> None:
        self._detector.stop()

    @override
    def _is_ready_impl(self) -> bool:
        return self._detector.is_ready()

    @override
    def preprocess(self, input: list[cv2t.MatLike]) -> list[cv2t.MatLike]:
        return self._detector.preprocess(input)

    @override
    def _predict_impl(
        self,
        input: list[cv2t.MatLike],
        *,
        preprocess: bool = True,
        **kwargs: Any,
    ) -> list[RawPersonDetection]:
        _EMPTY = RawPersonDetection(
            bbox_xyxy=np.zeros((0, 4), dtype=np.float32),
            confidence=np.zeros(0, dtype=np.float32),
        )

        orig_shapes = [(img.shape[0], img.shape[1]) for img in input]

        obj_results = self._detector.predict(
            input, preprocess=preprocess, classes=[_PERSON_CLASS_NAME],
        )

        results: list[RawPersonDetection] = []
        for i, det in enumerate(obj_results):
            if len(det.bbox_xywh) == 0:
                results.append(_EMPTY)
                continue

            H, W = orig_shapes[i]
            # bbox_xywh is [0,1] — convert to pixel xyxy for expansion, then normalize back
            pixel_xywh = det.bbox_xywh.copy()
            pixel_xywh[:, [0, 2]] *= W
            pixel_xywh[:, [1, 3]] *= H
            xyxy = xywh_to_xyxy(pixel_xywh, float(W), float(H))

            if self._bbox_expand_scale != 1.0:
                xyxy = expand_boxes_xyxy(xyxy, self._bbox_expand_scale, float(W), float(H))

            # Normalize to [0, 1]
            xyxy[:, [0, 2]] /= W
            xyxy[:, [1, 3]] /= H

            results.append(RawPersonDetection(
                bbox_xyxy=xyxy.astype(np.float32),
                confidence=det.confidence,
            ))

        return results
