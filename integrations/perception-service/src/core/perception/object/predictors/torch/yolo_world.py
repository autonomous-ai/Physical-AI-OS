"""YOLO-World zero-shot object detector."""

from pathlib import Path
from typing import Any

import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
import torch
from typing_extensions import override
from ultralytics import YOLOWorld

from core.models.object import RawObjectDetection
from core.utils.detection import xyxy_to_normalized_xywh

from core.perception.object.predictors.base import ObjectDetector


class YOLOWorldDetector(ObjectDetector):
    """Zero-shot object detection using YOLO-World (ultralytics); classes set per request."""

    DEFAULT_MODEL_PATH: Path | None = Path("yolov8s-worldv2.pt")

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
        classes_path: Path | None = None,
        threshold: float | None = None,
        batch_size: int | None = None,
    ) -> None:
        super().__init__(model_path=model_path, remote_url=remote_url, classes_path=classes_path, threshold=threshold, batch_size=batch_size)
        self._model: YOLOWorld | None = None
        self._running: bool = False

    @override
    def _start_impl(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._logger.info("Loading model from %s", self._model_path)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self._model = YOLOWorld(str(self._model_path)).to(device)
        self._class_names = self._load_classes(self._classes_path)
        self._running = True
        self._logger.info("Ready")

    @override
    def _stop_impl(self) -> None:
        self._model = None
        self._running = False
        self._logger.info("Stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._model is not None

    @override
    def preprocess(self, input: list[cv2t.MatLike]) -> list[cv2t.MatLike]:
        return input

    @override
    def _predict_impl(
        self,
        input: list[cv2t.MatLike],
        *,
        preprocess: bool = True,
        classes: list[str] | None = None,
        **kwargs: Any,
    ) -> list[RawObjectDetection]:
        if self._model is None:
            raise RuntimeError("Model not started")

        effective_classes: list[str] = classes if classes else self._class_names
        self._model.set_classes(effective_classes)

        results: list[RawObjectDetection] = []
        preds = self._model.predict(input, verbose=False, conf=self._threshold)

        for result in preds:
            boxes = result.boxes
            if boxes is None or len(boxes) == 0:
                results.append(RawObjectDetection(
                    bbox_xywh=np.zeros((0, 4), dtype=np.float32),
                    class_names=[],
                    confidence=np.zeros(0, dtype=np.float32),
                ))
                continue

            xyxy_np: npt.NDArray[np.float32] = boxes.xyxy.cpu().numpy().astype(np.float32)
            conf_np: npt.NDArray[np.float32] = boxes.conf.cpu().numpy().astype(np.float32)
            cls_np: npt.NDArray[np.int64] = boxes.cls.cpu().numpy().astype(np.int64)

            valid_np = cls_np < len(effective_classes)
            xyxy_np = xyxy_np[valid_np]
            conf_np = conf_np[valid_np]
            cls_np = cls_np[valid_np]

            # xyxy pixel → xywh normalized [0, 1]
            H, W = input[len(results)].shape[:2]
            xywh_np = xyxy_to_normalized_xywh(xyxy_np, float(W), float(H))

            names: list[str] = [effective_classes[i] for i in cls_np]

            results.append(RawObjectDetection(
                bbox_xywh=xywh_np,
                class_names=names,
                confidence=conf_np,
            ))

        return results
