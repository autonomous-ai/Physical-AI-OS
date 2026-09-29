"""OWLv2 zero-shot object detector using ONNX Runtime (CLIP text encoder in-graph)."""

from pathlib import Path

import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
from transformers import Owlv2Processor
from typing_extensions import override

from core.enums.files import ModelEnum
from core.utils.common import get_or_default
from core.utils.detection import owlv2_preprocess, unowlv2_boxes
from core.utils.files import get_default_cdn_url, get_default_model_path

from .base import ONNXObjectDetector


class OWLv2ONNXDetector(ONNXObjectDetector):
    """Zero-shot object detection using OWLv2 ONNX model."""

    DEFAULT_MODEL_PATH: Path | None = get_default_model_path(ModelEnum.OWLV2_ONNX)
    DEFAULT_REMOTE_URL: str | None = get_default_cdn_url(ModelEnum.OWLV2_ONNX)
    DEFAULT_THRESHOLD: float = 0.1
    DEFAULT_HF_MODEL_ID: str = "google/owlv2-large-patch14-ensemble"

    ONNX_INPUT_NAMES: list[str] = ["images", "class_tokens"]
    ONNX_OUTPUT_NAMES: list[str] = ["boxes", "probs", "labels"]
    WARMUP_IMGSZ: int = 640

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
        hf_model_id: str | None = None,
        classes_path: Path | None = None,
        threshold: float | None = None,
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
        self._hf_model_id: str = get_or_default(hf_model_id, self.DEFAULT_HF_MODEL_ID)
        self._processor: Owlv2Processor | None = None

    @override
    def _start_tokenizer(self) -> None:
        self._logger.info("Loading tokenizer from %s", self._hf_model_id)
        self._processor = Owlv2Processor.from_pretrained(self._hf_model_id)

    @override
    def _stop_impl(self) -> None:
        super()._stop_impl()
        self._processor = None

    @override
    def _is_ready_impl(self) -> bool:
        return super()._is_ready_impl() and self._processor is not None

    @override
    def preprocess(self, input: list[cv2t.MatLike]) -> list[cv2t.MatLike]:
        """BGR → padded square → CLIP-normalized CHW."""
        return [owlv2_preprocess(img)[0] for img in input]

    @override
    def _build_onnx_inputs(
        self,
        img_batch: npt.NDArray[np.float32],
        classes: list[str],
    ) -> dict[str, npt.NDArray]:
        if self._processor is None:
            raise RuntimeError("Tokenizer not loaded")

        text_queries = [f"a photo of {c}" for c in classes]
        tok = self._processor(text=text_queries, images=None, return_tensors="np")
        class_tokens: npt.NDArray[np.int64] = tok["input_ids"].astype(np.int64)

        return {
            self.ONNX_INPUT_NAMES[0]: img_batch,
            self.ONNX_INPUT_NAMES[1]: class_tokens,
        }

    @override
    def _to_orig_normalized(
        self,
        boxes_xywh: npt.NDArray[np.float32],
        orig_hw: tuple[int, int],
    ) -> npt.NDArray[np.float32]:
        return unowlv2_boxes(boxes_xywh, orig_hw)
