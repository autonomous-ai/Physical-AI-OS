"""YOLO-World zero-shot object detector using ONNX Runtime (CLIP text encoder in-graph)."""

from pathlib import Path

import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
from typing_extensions import override
from ultralytics.nn.text_model import build_text_model

from core.enums.files import ModelEnum
from core.utils.common import get_or_default
from core.utils.detection import letterbox, unletterbox_boxes
from core.utils.files import get_default_cdn_url, get_default_model_path

from .base import ONNXObjectDetector


class YOLOWorldONNXDetector(ONNXObjectDetector):
    """Zero-shot object detection using YOLO-World ONNX model."""

    DEFAULT_MODEL_PATH: Path | None = get_default_model_path(ModelEnum.YOLO_WORLD_ONNX)
    DEFAULT_REMOTE_URL: str | None = get_default_cdn_url(ModelEnum.YOLO_WORLD_ONNX)
    DEFAULT_THRESHOLD: float = 0.25
    DEFAULT_IMGSZ: int = 640
    DEFAULT_TEXT_MODEL: str = "clip:ViT-B/32"

    ONNX_INPUT_NAMES: list[str] = ["images", "class_tokens"]
    ONNX_OUTPUT_NAMES: list[str] = ["boxes", "probs", "labels"]

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
        text_model_name: str | None = None,
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
        self._text_model_name: str = get_or_default(text_model_name, self.DEFAULT_TEXT_MODEL)
        self._imgsz: int = get_or_default(imgsz, self.DEFAULT_IMGSZ)
        self._clip_model = None

    @override
    def _start_tokenizer(self) -> None:
        self._logger.info("Loading CLIP text encoder: %s", self._text_model_name)
        self._clip_model = build_text_model(self._text_model_name)

    @override
    def _stop_impl(self) -> None:
        super()._stop_impl()
        self._clip_model = None

    @override
    def _is_ready_impl(self) -> bool:
        return super()._is_ready_impl() and self._clip_model is not None

    @override
    def preprocess(self, input: list[cv2t.MatLike]) -> list[cv2t.MatLike]:
        """BGR → letterbox to imgsz → RGB CHW [0, 1]."""
        return [letterbox(img, target_size=self._imgsz)[0] for img in input]

    @override
    def _build_onnx_inputs(
        self,
        img_batch: npt.NDArray[np.float32],
        classes: list[str],
    ) -> dict[str, npt.NDArray]:
        if self._clip_model is None:
            raise RuntimeError("CLIP text encoder not loaded")

        class_tokens = self._clip_model.tokenize(classes)
        class_tokens_np: npt.NDArray[np.int32] = np.asarray(
            class_tokens, dtype=np.int32
        )

        return {
            self.ONNX_INPUT_NAMES[0]: img_batch,
            self.ONNX_INPUT_NAMES[1]: class_tokens_np,
        }

    @override
    def _to_orig_normalized(
        self,
        boxes_xywh: npt.NDArray[np.float32],
        orig_hw: tuple[int, int],
    ) -> npt.NDArray[np.float32]:
        return unletterbox_boxes(boxes_xywh, orig_hw, self._imgsz)
