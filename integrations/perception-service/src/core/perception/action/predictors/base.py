"""Abstract base class for human action recognizer ONNX models."""

from pathlib import Path
from typing import Any, cast

import cv2
import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
import onnxruntime as ort
from typing_extensions import override

from core.models.action import RawHumanActionDetection
from core.models.media import Video
from core.perception.action.constants import RESOURCES_DIR
from core.perception.base import PredictorBase
from core.utils.common import get_or_default
from core.utils.files import ensure_downloaded
from core.utils.runtime import prepare_ort_session


class HumanActionRecognizer(PredictorBase[Video, RawHumanActionDetection]):
    """Base interface for all action recognition ONNX models."""

    DEFAULT_MODEL_PATH: Path | None = None
    DEFAULT_REMOTE_URL: str | None = None
    DEFAULT_CLASSES_PATH: Path = RESOURCES_DIR / "kinect_classes.txt"
    DEFAULT_WHITELIST_PATH: Path | None = RESOURCES_DIR / "white_list.txt"

    DEFAULT_MAX_FRAMES: int = 8
    DEFAULT_FRAME_SIZE: tuple[int, int] = (224, 224)
    ONNX_INPUT_NAME: str = "input"
    ONNX_OUTPUT_NAME: str = "pred"

    MEAN: npt.NDArray[np.float32] = np.array([0, 0, 0], dtype=np.float32)
    STD: npt.NDArray[np.float32] = np.array([1, 1, 1], dtype=np.float32)

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
        classes_path: Path | None = None,
        whitelist_path: Path | None = None,
        max_frames: int | None = None,
        frame_size: tuple[int, int] | None = None,
        batch_size: int | None = None,
    ):
        super().__init__(batch_size=batch_size)

        model_path = get_or_default(model_path, self.DEFAULT_MODEL_PATH)
        if model_path is None:
            raise RuntimeError("model_path are not allowed to be None")

        self._model_path: Path = model_path
        self._remote_url: str | None = get_or_default(remote_url, self.DEFAULT_REMOTE_URL)
        self._classes_path: Path = get_or_default(classes_path, self.DEFAULT_CLASSES_PATH)
        self._whitelist_path: Path | None = get_or_default(
            whitelist_path, self.DEFAULT_WHITELIST_PATH
        )

        self._max_frames: int = get_or_default(max_frames, self.DEFAULT_MAX_FRAMES)
        self._frame_size: tuple[int, int] = get_or_default(frame_size, self.DEFAULT_FRAME_SIZE)

        self._class_names: list[str] = []
        self._default_class_mask: npt.NDArray[np.bool_] = np.ones(0, dtype=np.bool_)

        self._running: bool = False
        self._session: ort.InferenceSession | None = None

    @property
    def class_names(self) -> list[str]:
        return self._class_names

    @property
    def default_class_mask(self) -> npt.NDArray[np.bool_]:
        return self._default_class_mask

    @property
    def max_frames(self) -> int:
        return self._max_frames

    @property
    def frame_size(self) -> tuple[int, int]:
        return self._frame_size

    @override
    def predict(
        self,
        input: list[Video],
        *,
        preprocess: bool = True,
        class_mask: npt.NDArray[np.bool_] | None = None,
        **kwargs: Any,
    ) -> list[RawHumanActionDetection]:
        return super().predict(input, preprocess=preprocess, class_mask=class_mask)

    @override
    def _start_impl(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return

        self._model_path = ensure_downloaded(self._model_path, remote=self._remote_url)
        self._logger.info("Loading model from %s", self._model_path)
        H, W = self._frame_size
        warmup = {self.ONNX_INPUT_NAME: np.zeros(
            (self._batch_size, 1, 3, self._max_frames, H, W), dtype=np.float32,
        )}
        self._session = prepare_ort_session(self._model_path, warmup_inputs=warmup)
        self._class_names, self._default_class_mask = self._load_classes(
            self._classes_path, self._whitelist_path
        )

        self._running = True

        self._logger.info(
            "Predictor started - %d classes, %d whitelisted",
            len(self._class_names),
            int(self._default_class_mask.sum()),
        )

    @override
    def _stop_impl(self) -> None:
        self._session = None
        self._running = False
        self._logger.info("Predictor stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._session is not None

    @override
    def _predict_impl(
        self,
        input: list[Video],
        *,
        preprocess: bool = True,
        class_mask: npt.NDArray[np.bool_] | None = None,
        **kwargs: Any,
    ) -> list[RawHumanActionDetection]:
        """Run inference on buffered frames, return raw prediction (numpy arrays)."""
        if self._session is None:
            msg = f"{self.__class__.__name__} session cannot be None"
            raise RuntimeError(msg)

        if preprocess:
            input = self.preprocess(input)

        input_np: npt.NDArray[np.float32] = np.array([i.frames for i in input], dtype=np.float32)
        N, T, H, W, C = input_np.shape
        input_np = (input_np - self.MEAN) / self.STD

        # The model needs exactly `max_frames` along the temporal axis (axis=1).
        if T < self._max_frames:
            input_np = np.pad(
                input_np,
                ((0, 0), (0, self._max_frames - T), (0, 0), (0, 0), (0, 0)),
                mode="constant",
                constant_values=0,
            )
        else:
            input_np = input_np[-self._max_frames :]

        input_np = input_np.transpose(0, 4, 1, 2, 3)  # (N, C, T, H, W)
        input_np = input_np[:, np.newaxis, ...]  # (N, 1, C, T, H, W)

        if class_mask is not None:
            class_mask = np.logical_and(class_mask, self._default_class_mask)
        else:
            class_mask = self._default_class_mask

        with self._gpu_lock:
            (probs,) = self._session.run([self.ONNX_OUTPUT_NAME], {self.ONNX_INPUT_NAME: input_np})

        probs = cast(npt.NDArray[np.float32], probs)  # (N, C)
        probs = self._postprocess_probs(probs)
        probs[:, ~class_mask] = 0

        return [RawHumanActionDetection(prob_np=prob) for prob in probs]

    def _postprocess_probs(self, probs: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
        """Post-process ONNX output probabilities. No-op for models that bake
        softmax into the ONNX graph (e.g. UniformerV2). Subclasses whose ONNX
        outputs raw logits (X3D, VideoMAE) override to apply softmax."""
        return probs

    def preprocess_single_frame(
        self,
        frame: cv2t.MatLike,
    ) -> cv2t.MatLike:
        """Resize and center-crop a single frame. Used by session for buffering."""
        frame_rgb: cv2t.MatLike = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        H, W = frame_rgb.shape[:2]
        target_h, target_w = self._frame_size
        r: float = max(target_w / W, target_h / H)
        resized: cv2t.MatLike = cv2.resize(frame_rgb, None, fx=r, fy=r)
        nh, nw = resized.shape[:2]
        half_h, half_w = target_h // 2, target_w // 2
        return resized[nh // 2 - half_h : nh // 2 + half_h, nw // 2 - half_w : nw // 2 + half_w]

    @override
    def preprocess(
        self,
        input: list[Video],
    ) -> list[Video]:
        """Resize and center-crop each frame in each video."""
        return [
            Video(
                frames=[self.preprocess_single_frame(frame) for frame in video.frames],
                fps=video.fps,
            )
            for video in input
        ]

    def _load_classes(
        self, classes_path: Path, whitelist_path: Path | None
    ) -> tuple[list[str], npt.NDArray[np.bool_]]:
        class_names = classes_path.read_text().strip().split("\n")
        mask = np.ones(len(class_names), dtype=np.bool_)

        if whitelist_path is not None and whitelist_path.exists():
            whitelist = set(whitelist_path.read_text().strip().split("\n"))
            mask = np.array([name in whitelist for name in class_names], dtype=np.bool_)

        return class_names, mask
