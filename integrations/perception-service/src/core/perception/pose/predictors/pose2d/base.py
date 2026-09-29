"""Abstract base class for 2D pose estimators."""

from pathlib import Path
from typing import Any, cast

import cv2
import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
import onnxruntime as ort
from typing_extensions import override

from core.enums.pose import GraphEnum
from core.models.pose import RawPose2DDetection
from core.perception.base import PredictorBase
from core.utils.common import get_or_default
from core.utils.files import ensure_downloaded
from core.utils.runtime import prepare_ort_session


class PoseEstimator2D(PredictorBase[cv2t.MatLike, RawPose2DDetection]):
    """Base class for 2D pose estimators (e.g. RTMPose); subclasses override class-level defaults."""

    GRAPH_TYPE: GraphEnum

    DEFAULT_MODEL_PATH: Path | None = None
    DEFAULT_REMOTE_URL: str | None = None
    DEFAULT_INPUT_SIZE: tuple[int, int] = (192, 256)
    ONNX_INPUT_NAME: str = "input"
    ONNX_OUTPUT_NAMES: list[str] = ["simcc_x", "simcc_y"]

    INPUT_MEAN: npt.NDArray[np.float32] = np.array([123.675, 116.28, 103.53], dtype=np.float32)
    INPUT_STD: npt.NDArray[np.float32] = np.array([58.395, 57.12, 57.375], dtype=np.float32)

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
        input_size: tuple[int, int] | None = None,
        batch_size: int | None = None,
    ) -> None:
        super().__init__(batch_size=batch_size)

        model_path = get_or_default(model_path, self.DEFAULT_MODEL_PATH)
        if model_path is None:
            raise RuntimeError("model_path must not be None")

        self._model_path: Path = model_path
        self._remote_url: str | None = get_or_default(remote_url, self.DEFAULT_REMOTE_URL)
        self._input_size: tuple[int, int] = get_or_default(input_size, self.DEFAULT_INPUT_SIZE)

        self._session: ort.InferenceSession | None = None
        self._running: bool = False

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
        W, H = self._input_size
        warmup = {self.ONNX_INPUT_NAME: np.zeros((self._batch_size, 3, H, W), dtype=np.float32)}
        self._session = prepare_ort_session(self._model_path, warmup_inputs=warmup)
        self._running = True
        self._logger.info("Ready")

    @override
    def _stop_impl(self) -> None:
        self._session = None
        self._running = False

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._session is not None

    @override
    def preprocess(self, input: list[cv2t.MatLike]) -> list[npt.NDArray[np.float32]]:
        """Resize, normalize, CHW for each frame. Returns list of (1, C, H, W) tensors."""
        W, H = self._input_size
        results: list[npt.NDArray[np.float32]] = []
        for frame in input:
            img: npt.NDArray[np.float32] = cv2.resize(frame, (W, H)).astype(np.float32)
            img = (img - self.INPUT_MEAN) / self.INPUT_STD
            img = img.transpose(2, 0, 1)  # HWC → CHW
            results.append(img[np.newaxis].astype(np.float32))  # (1, C, H, W)
        return results

    @override
    def _predict_impl(
        self,
        input: list[cv2t.MatLike],
        *,
        preprocess: bool = True,
        **kwargs: Any,
    ) -> list[RawPose2DDetection]:
        """Run 2D pose estimation on a batch of frames; one RawPose2DDetection per frame."""
        original_sizes: npt.NDArray[np.float32] = np.array(
            [(f.shape[1], f.shape[0]) for f in input], dtype=np.float32
        )  # (N, 2)

        preprocessed: list[npt.NDArray[np.float32]]
        if preprocess:
            preprocessed = self.preprocess(input)
        else:
            preprocessed = [cast(npt.NDArray[np.float32], inp) for inp in input]

        # Batch inference: stack (1, C, H, W) → (N, C, H, W)
        batch: npt.NDArray[np.float32] = np.concatenate(preprocessed, axis=0)
        with self._gpu_lock:        
            simcc_x, simcc_y = self._session.run(self.ONNX_OUTPUT_NAMES, {self.ONNX_INPUT_NAME: batch})
        simcc_x = np.asarray(simcc_x, dtype=np.float32)  # (N, K, Lx)
        simcc_y = np.asarray(simcc_y, dtype=np.float32)  # (N, K, Ly)

        IW, IH = self._input_size
        OW: npt.NDArray[np.float32] = original_sizes[:, 0:1]  # (N, 1)
        OH: npt.NDArray[np.float32] = original_sizes[:, 1:2]  # (N, 1)

        # SimCC decode to original-frame pixels: argmax bin * 0.5 (= 1/simcc_split_ratio,
        # RTMPose uses 2.0) * OW / IW. If the split ratio changes, 0.5 must change too.
        loc_x: npt.NDArray[np.float32] = simcc_x.argmax(-1).astype(np.float32) * OW / IW * 0.5
        loc_y: npt.NDArray[np.float32] = simcc_y.argmax(-1).astype(np.float32) * OH / IH * 0.5
        all_keypoints: npt.NDArray[np.float32] = np.stack([loc_x, loc_y], axis=-1)  # (N, K, 2)
        # Confidence = weaker of the x/y axis peaks.
        all_scores: npt.NDArray[np.float32] = np.minimum(simcc_x.max(-1), simcc_y.max(-1)).astype(
            np.float32
        )  # (N, K)

        return [
            RawPose2DDetection(keypoints=all_keypoints[i : i + 1], scores=all_scores[i : i + 1])
            for i in range(len(input))
        ]
