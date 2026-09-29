"""ONNX base class for object detectors."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, cast

import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
import onnxruntime as ort
from typing_extensions import override

from core.models.object import RawObjectDetection
from core.perception.object.predictors.base import ObjectDetector
from core.utils.detection import nms_xywh
from core.utils.files import ensure_downloaded
from core.utils.runtime import prepare_ort_session


class ONNXObjectDetector(ObjectDetector, ABC):
    """Base for ONNX object detectors: lifecycle, batched inference, padding/threshold/NMS postprocess.

    Subclasses configure I/O names, preprocessing and tokenization.
    """

    ONNX_INPUT_NAMES: list[str] = ["images"]
    ONNX_OUTPUT_NAMES: list[str] = ["boxes", "probs", "labels"]
    WARMUP_IMGSZ: int = 640

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
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
        )
        self._nms: bool = nms
        self._session: ort.InferenceSession | None = None
        self._running: bool = False

    @override
    def _start_impl(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._model_path = ensure_downloaded(self._model_path, remote=self._remote_url)
        self._logger.info("Loading ONNX model from %s", self._model_path)
        self._start_tokenizer()
        self._class_names = self._load_classes(self._classes_path)

        warmup = self._build_warmup_inputs()
        self._session = prepare_ort_session(self._model_path, warmup_inputs=warmup)

        self._running = True
        self._logger.info(
            "Ready — %d default classes, nms=%s", len(self._class_names), self._nms
        )

    def _build_warmup_inputs(self) -> dict[str, np.ndarray] | None:
        """Build dummy ONNX inputs for warmup inference at startup."""
        try:
            sz = self.WARMUP_IMGSZ
            dummy = np.zeros((sz, sz, 3), dtype=np.uint8)
            batch = [dummy] * self._batch_size
            preprocessed = self.preprocess(batch)
            img_batch = np.stack(preprocessed, axis=0)
            classes = self._class_names[:1] or ["dummy"]
            return self._build_onnx_inputs(img_batch, classes)
        except Exception as e:
            self._logger.warning("Could not build warmup inputs: %s", e)
            return None

    def _start_tokenizer(self) -> None:
        """Load a tokenizer/processor for text encoding; no-op by default."""

    @override
    def _stop_impl(self) -> None:
        self._session = None
        self._running = False
        self._logger.info("Stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._session is not None

    @abstractmethod
    def _build_onnx_inputs(
        self,
        img_batch: npt.NDArray[np.float32],
        classes: list[str],
    ) -> dict[str, npt.NDArray]:
        """Build the ONNX session input dict from preprocessed images + (tokenized) classes."""

    @abstractmethod
    def _to_orig_normalized(
        self,
        boxes_xywh: npt.NDArray[np.float32],
        orig_hw: tuple[int, int],
    ) -> npt.NDArray[np.float32]:
        """Convert model-space normalized xywh to original-image normalized xywh."""

    @override
    def _predict_impl(
        self,
        input: list[cv2t.MatLike],
        *,
        preprocess: bool = True,
        classes: list[str] | None = None,
        **kwargs: Any,
    ) -> list[RawObjectDetection]:
        if self._session is None:
            raise RuntimeError(f"{self.__class__.__name__} is not ready")

        effective_classes: list[str] = classes if classes else self._class_names

        orig_hws: list[tuple[int, int]] = [(img.shape[0], img.shape[1]) for img in input]

        if preprocess:
            input = self.preprocess(input)

        img_batch: npt.NDArray[np.float32] = np.stack(input, axis=0)

        onnx_inputs = self._build_onnx_inputs(img_batch, effective_classes)
        with self._gpu_lock:
            raw_outputs = self._session.run(self.ONNX_OUTPUT_NAMES, onnx_inputs)

        return self._postprocess_batch(raw_outputs, effective_classes, orig_hws)

    def _get_label_pool(self, classes: list[str]) -> list[str]:
        """Class list that label indices map into: ``classes`` for zero-shot models.

        Fixed-class subclasses override to return ``self._class_names``.
        """
        return classes

    def _postprocess_batch(
        self,
        raw_outputs: list[npt.NDArray],
        classes: list[str],
        orig_hws: list[tuple[int, int]],
    ) -> list[RawObjectDetection]:
        """Convert batched ONNX outputs to per-image detections (xywh normalized to the original image)."""
        all_boxes = cast(npt.NDArray[np.float32], raw_outputs[0])   # [B, N, 4]
        all_probs = cast(npt.NDArray[np.float32], raw_outputs[1])   # [B, N, K]
        all_labels = cast(npt.NDArray[np.int64], raw_outputs[2])    # [B, N]

        label_pool = self._get_label_pool(classes)
        requested = set(classes)

        results: list[RawObjectDetection] = []
        for i in range(all_boxes.shape[0]):
            boxes = all_boxes[i]
            probs = all_probs[i]
            labels = all_labels[i]

            valid = (labels >= 0) & (labels < len(label_pool))
            boxes = boxes[valid]
            probs = probs[valid]
            labels = labels[valid]

            if len(boxes) == 0:
                results.append(RawObjectDetection(
                    bbox_xywh=np.zeros((0, 4), dtype=np.float32),
                    class_names=[],
                    confidence=np.zeros(0, dtype=np.float32),
                ))
                continue

            conf = np.array(
                [probs[j, labels[j]] for j in range(len(labels))],
                dtype=np.float32,
            )

            keep = conf >= self._threshold
            boxes = boxes[keep]
            labels = labels[keep]
            conf = conf[keep]

            names = [label_pool[int(idx)] for idx in labels]

            if requested != set(label_pool):
                keep_cls = [i for i, n in enumerate(names) if n in requested]
                if not keep_cls:
                    results.append(RawObjectDetection(
                        bbox_xywh=np.zeros((0, 4), dtype=np.float32),
                        class_names=[],
                        confidence=np.zeros(0, dtype=np.float32),
                    ))
                    continue
                boxes = boxes[keep_cls]
                conf = conf[keep_cls]
                names = [names[i] for i in keep_cls]

            if self._nms and len(boxes) > 0:
                keep_nms = nms_xywh(boxes, conf)
                boxes = boxes[keep_nms]
                conf = conf[keep_nms]
                names = [names[i] for i in keep_nms]

            orig_hw = orig_hws[i]
            norm_xywh = self._to_orig_normalized(boxes.astype(np.float32), orig_hw)

            results.append(RawObjectDetection(
                bbox_xywh=norm_xywh,
                class_names=names,
                confidence=conf,
            ))

        return results
