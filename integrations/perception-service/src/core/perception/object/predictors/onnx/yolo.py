"""YOLO COCO object detector using ONNX Runtime."""

from pathlib import Path

import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
from typing_extensions import override

from core.enums.files import ModelEnum
from core.perception.object.constants import RESOURCES_DIR
from core.utils.common import get_or_default
from core.utils.detection import letterbox, unletterbox_boxes
from core.utils.files import get_default_cdn_url, get_default_model_path

from .base import ONNXObjectDetector


class YOLOONNXDetector(ONNXObjectDetector):
    """COCO object detection using YOLO ONNX model."""

    DEFAULT_MODEL_PATH: Path | None = get_default_model_path(ModelEnum.YOLO_PERSON_ONNX)
    DEFAULT_REMOTE_URL: str | None = get_default_cdn_url(ModelEnum.YOLO_PERSON_ONNX)
    DEFAULT_CLASSES_PATH: Path = RESOURCES_DIR / "coco_classes.txt"
    DEFAULT_THRESHOLD: float = 0.4
    DEFAULT_IMGSZ: int = 640

    ONNX_INPUT_NAMES: list[str] = ["images"]
    ONNX_OUTPUT_NAMES: list[str] = ["boxes", "probs", "labels"]

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
        classes_path: Path | None = None,
        threshold: float | None = None,
        imgsz: int | None = None,
        batch_size: int | None = None,
        nms: bool = True,
    ) -> None:
        super().__init__(
            model_path=model_path,
            remote_url=remote_url,
            classes_path=classes_path,
            threshold=threshold,
            batch_size=batch_size,
            nms=nms,
        )
        self._imgsz: int = get_or_default(imgsz, self.DEFAULT_IMGSZ)

    @override
    def preprocess(self, input: list[cv2t.MatLike]) -> list[cv2t.MatLike]:
        """BGR → letterbox to imgsz → RGB CHW [0, 1]."""
        return [letterbox(img, target_size=self._imgsz)[0] for img in input]

    @override
    def _get_label_pool(self, classes: list[str]) -> list[str]:
        """YOLO labels index into the full COCO class list, not the user query."""
        return self._class_names

    @override
    def _build_onnx_inputs(
        self,
        img_batch: npt.NDArray[np.float32],
        classes: list[str],
    ) -> dict[str, npt.NDArray]:
        return {self.ONNX_INPUT_NAMES[0]: img_batch}

    @override
    def _to_orig_normalized(
        self,
        boxes_xywh: npt.NDArray[np.float32],
        orig_hw: tuple[int, int],
    ) -> npt.NDArray[np.float32]:
        return unletterbox_boxes(boxes_xywh, orig_hw, self._imgsz)
