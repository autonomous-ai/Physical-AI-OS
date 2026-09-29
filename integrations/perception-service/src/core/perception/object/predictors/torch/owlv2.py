"""OWLv2 zero-shot object detector (HuggingFace transformers)."""

from pathlib import Path
from typing import Any

import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
import torch
from PIL import Image
from transformers import Owlv2ForObjectDetection, Owlv2Processor
from typing_extensions import override

from core.models.object import RawObjectDetection
from core.utils.detection import xyxy_to_normalized_xywh

from core.perception.object.predictors.base import ObjectDetector


class OWLv2Detector(ObjectDetector):
    """Zero-shot object detection using OWLv2; text queries are built per request."""

    DEFAULT_MODEL_PATH: Path | None = Path("google/owlv2-large-patch14-ensemble")
    DEFAULT_THRESHOLD: float = 0.1

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
        classes_path: Path | None = None,
        threshold: float | None = None,
        batch_size: int | None = None,
    ) -> None:
        super().__init__(model_path=model_path, remote_url=remote_url, classes_path=classes_path, threshold=threshold, batch_size=batch_size)
        self._processor: Owlv2Processor | None = None
        self._model: Owlv2ForObjectDetection | None = None
        self._device: str = ""
        self._running: bool = False

    @override
    def _start_impl(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._logger.info("Loading model from %s", self._model_path)
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        self._processor = Owlv2Processor.from_pretrained(str(self._model_path))
        self._model = Owlv2ForObjectDetection.from_pretrained(str(self._model_path)).to(
            self._device
        )
        self._class_names = self._load_classes(self._classes_path)
        self._running = True
        self._logger.info("Ready")

    @override
    def _stop_impl(self) -> None:
        self._model = None
        self._processor = None
        self._running = False
        self._logger.info("Stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._model is not None and self._processor is not None

    @override
    def preprocess(self, input: list[cv2t.MatLike]) -> list[cv2t.MatLike]:
        return input

    @torch.no_grad()
    @override
    def _predict_impl(
        self,
        input: list[cv2t.MatLike],
        *,
        preprocess: bool = True,
        classes: list[str] | None = None,
        **kwargs: Any,
    ) -> list[RawObjectDetection]:
        if self._model is None or self._processor is None:
            raise RuntimeError("Model not started")

        effective_classes: list[str] = classes if classes else self._class_names
        text_queries: list[str] = [f"a photo of {c}" for c in effective_classes]

        pil_images: list[Image.Image] = [
            Image.fromarray(img[:, :, ::-1]) for img in input
        ]
        target_sizes: list[tuple[int, int]] = [
            (img.shape[0], img.shape[1]) for img in input
        ]

        inputs = self._processor(
            text=text_queries, images=pil_images, return_tensors="pt"
        ).to(self._device)

        self._model.eval()
        outputs = self._model(**inputs)

        target_sizes_tensor = torch.tensor(target_sizes, device=self._device)
        batch_results = self._processor.post_process_grounded_object_detection(
            outputs=outputs,
            target_sizes=target_sizes_tensor,
            threshold=self._threshold,
        )

        results: list[RawObjectDetection] = []
        for post in batch_results:
            xyxy_np: npt.NDArray[np.float32] = post["boxes"].cpu().numpy().astype(np.float32)
            conf_np: npt.NDArray[np.float32] = post["scores"].cpu().numpy().astype(np.float32)
            labels_np: npt.NDArray[np.int64] = post["labels"].cpu().numpy().astype(np.int64)

            valid_np = labels_np < len(effective_classes)
            xyxy_np = xyxy_np[valid_np]
            conf_np = conf_np[valid_np]
            labels_np = labels_np[valid_np]

            if len(xyxy_np) == 0:
                results.append(RawObjectDetection(
                    bbox_xywh=np.zeros((0, 4), dtype=np.float32),
                    class_names=[],
                    confidence=np.zeros(0, dtype=np.float32),
                ))
                continue

            # xyxy pixel → xywh normalized [0, 1]
            H, W = input[len(results)].shape[:2]
            xywh_np = xyxy_to_normalized_xywh(xyxy_np, W, H)

            names: list[str] = [effective_classes[i] for i in labels_np]

            results.append(RawObjectDetection(
                bbox_xywh=xywh_np,
                class_names=names,
                confidence=conf_np,
            ))

        return results
