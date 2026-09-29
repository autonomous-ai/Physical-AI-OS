"""Base emotion predictor: classifies emotion from face crops (detection is done by the session)."""

from pathlib import Path
from typing import Any, cast

import cv2
import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
import onnxruntime as ort
from typing_extensions import override

from core.models.facial_emotion import RawEmotionDetection
from core.perception.base import PredictorBase
from core.perception.facial_emotion.constants import RESOURCES_DIR
from core.utils.common import get_or_default
from core.utils.files import ensure_downloaded
from core.utils.runtime import prepare_ort_session


class EmotionRecognizer(PredictorBase[cv2t.MatLike, RawEmotionDetection]):
    """Base class for emotion classifiers on face crops; subclasses override class-level defaults."""

    DEFAULT_MODEL_PATH: Path | None = None
    DEFAULT_REMOTE_URL: str | None = None
    DEFAULT_CLASSES_PATH: Path = RESOURCES_DIR / "posterv2_classes.txt"
    DEFAULT_INPUT_SIZE: tuple[int, int] = (224, 224)
    ONNX_INPUT_NAME: str = "images"

    MEAN: npt.NDArray[np.float32] = np.array([0, 0, 0], dtype=np.float32)
    STD: npt.NDArray[np.float32] = np.array([1, 1, 1], dtype=np.float32)

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
        classes_path: Path | None = None,
        input_size: tuple[int, int] | None = None,
        batch_size: int | None = None,
    ) -> None:
        super().__init__(batch_size=batch_size)

        model_path = get_or_default(model_path, self.DEFAULT_MODEL_PATH)
        if model_path is None:
            raise RuntimeError("model_path must not be None")

        self._model_path: Path = model_path
        self._remote_url: str | None = get_or_default(remote_url, self.DEFAULT_REMOTE_URL)
        self._classes_path: Path = get_or_default(classes_path, self.DEFAULT_CLASSES_PATH)
        self._input_size: tuple[int, int] = get_or_default(input_size, self.DEFAULT_INPUT_SIZE)

        self._class_names: list[str] = []
        self._running: bool = False
        self._session: ort.InferenceSession | None = None

    @property
    def class_names(self) -> list[str]:
        return self._class_names

    @property
    def input_size(self) -> tuple[int, int]:
        return self._input_size

    @override
    def _start_impl(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._model_path = ensure_downloaded(self._model_path, remote=self._remote_url)
        self._logger.info("Loading model from %s", self._model_path)
        H, W = self._input_size
        warmup = {self.ONNX_INPUT_NAME: np.zeros((self._batch_size, 3, H, W), dtype=np.float32)}
        self._session = prepare_ort_session(self._model_path, warmup_inputs=warmup)
        self._class_names = self._load_classes(self._classes_path)
        self._running = True
        self._logger.info("Ready — %d emotion classes", len(self._class_names))

    @override
    def _stop_impl(self) -> None:
        self._session = None
        self._running = False
        self._logger.info("Stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._session is not None

    @override
    def preprocess(self, input: list[cv2t.MatLike]) -> list[cv2t.MatLike]:
        """Default preprocessing: resize, BGR→RGB, normalize, CHW, add batch dim."""
        H, W = self._input_size
        results: list[cv2t.MatLike] = []
        for face_crop in input:
            resized: cv2t.MatLike = cv2.resize(face_crop, (W, H))
            rgb: cv2t.MatLike = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
            results.append(rgb)
        return results

    @override
    def _predict_impl(
        self,
        input: list[cv2t.MatLike],
        *,
        preprocess: bool = True,
        **kwargs: Any,
    ) -> list[RawEmotionDetection]:
        """Classify emotion for a batch of BGR face crops; one RawEmotionDetection per crop.

        Args:
            preprocess: False when crops are already preprocessed.
        """
        if self._session is None:
            raise RuntimeError(f"{self.__class__.__name__} is not ready")

        if preprocess:
            input = self.preprocess(input)

        input_np = np.array(input, dtype=np.float32)
        N, H, W, C = input_np.shape
        input_np = (input_np / 255.0 - self.MEAN) / self.STD
        input_np = input_np.transpose(0, 3, 1, 2)  # (N, C, H, W)

        raw_outputs: list[npt.NDArray[np.float32]] = self._session.run(None, {self.ONNX_INPUT_NAME: input_np})

        return self._postprocess_batch(raw_outputs, len(input))

    def _postprocess_batch(
        self, raw_outputs: list[npt.NDArray[np.float32]], N: int
    ) -> list[RawEmotionDetection]:
        """Convert batched ONNX output (first output = probs (N, C), softmax baked in) to detections."""
        probs: npt.NDArray[np.float32] = cast(npt.NDArray[np.float32], raw_outputs[0])

        return [RawEmotionDetection(expression_probs=probs[i]) for i in range(N)]

    @staticmethod
    def _load_classes(classes_path: Path) -> list[str]:
        return classes_path.read_text().strip().split("\n")
