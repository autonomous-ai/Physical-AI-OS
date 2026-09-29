"""YuNet face detector using OpenCV's FaceDetectorYN."""

from pathlib import Path
from typing import Any

import cv2
import cv2.typing as cv2t
import numpy as np
from typing_extensions import override

from core.enums.files import ModelEnum
from core.models.face import RawFaceDetection
from core.perception.face.predictors.base import FaceDetector
from core.utils.common import get_or_default
from core.utils.files import ensure_downloaded, get_default_cdn_url, get_default_model_path


class YuNetFaceDetector(FaceDetector):
    """YuNet-based face detector using OpenCV's FaceDetectorYN."""

    DEFAULT_MODEL_PATH: Path = get_default_model_path(ModelEnum.YUNET_ONNX)
    DEFAULT_REMOTE_URL: str | None = get_default_cdn_url(ModelEnum.YUNET_ONNX)
    DEFAULT_INPUT_SIZE: tuple[int, int] = (320, 320)
    DEFAULT_SCORE_THRESHOLD: float = 0.7
    DEFAULT_NMS_THRESHOLD: float = 0.3
    DEFAULT_TOP_K: int = 5000

    def __init__(
        self,
        model_path: Path | None = None,
        remote_url: str | None = None,
        input_size: tuple[int, int] | None = None,
        score_threshold: float | None = None,
        nms_threshold: float | None = None,
        top_k: int | None = None,
        batch_size: int | None = None,
    ) -> None:
        super().__init__(batch_size=batch_size)
        self._model_path: Path = get_or_default(model_path, self.DEFAULT_MODEL_PATH)
        self._remote_url: str | None = get_or_default(remote_url, self.DEFAULT_REMOTE_URL)
        self._input_size: tuple[int, int] = get_or_default(input_size, self.DEFAULT_INPUT_SIZE)
        self._score_threshold: float = get_or_default(score_threshold, self.DEFAULT_SCORE_THRESHOLD)
        self._nms_threshold: float = get_or_default(nms_threshold, self.DEFAULT_NMS_THRESHOLD)
        self._top_k: int = get_or_default(top_k, self.DEFAULT_TOP_K)
        self._detector: cv2.FaceDetectorYN | None = None
        self._running: bool = False

    @override
    def _start_impl(self) -> None:
        if self._running:
            self._logger.info("Already running")
            return
        self._model_path = ensure_downloaded(self._model_path, remote=self._remote_url)
        self._logger.info("Loading model from %s", self._model_path)
        self._detector = cv2.FaceDetectorYN.create(
            str(self._model_path),
            "",
            self._input_size,
            self._score_threshold,
            self._nms_threshold,
            self._top_k,
        )
        self._running = True
        self._logger.info("Ready")

    @override
    def _stop_impl(self) -> None:
        self._detector = None
        self._running = False

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and self._detector is not None

    @override
    def preprocess(self, input: list[cv2t.MatLike]) -> list[cv2t.MatLike]:
        """No preprocessing needed — YuNet handles resize via setInputSize."""
        return input

    @override
    def _predict_impl(
        self, input: list[cv2t.MatLike], *, preprocess: bool = True, **kwargs: Any
    ) -> list[RawFaceDetection]:
        """Detect faces in a batch of BGR frames; one RawFaceDetection per frame."""
        _EMPTY: RawFaceDetection = RawFaceDetection(
            bbox_xyxy=np.zeros((0, 4), dtype=np.float32),
            confidence=np.zeros(0, dtype=np.float32),
        )

        results: list[RawFaceDetection] = []
        for frame in input:
            H, W = frame.shape[:2]
            self._detector.setInputSize((W, H))
            _, faces = self._detector.detect(frame)

            if faces is None or len(faces) == 0:
                results.append(_EMPTY)
                continue

            # YuNet output: each row is [x, y, w, h, ..., confidence_at_index_14]
            # Convert xywh → xyxy
            bbox_xyxy_list: list[list[int]] = []
            conf_list: list[float] = []

            for face in faces:
                x, y, fw, fh = face[:4].astype(int)
                conf: float = float(face[14])

                x1: int = max(0, x)
                y1: int = max(0, y)
                x2: int = min(W, x + fw)
                y2: int = min(H, y + fh)

                if x2 <= x1 or y2 <= y1:
                    continue

                bbox_xyxy_list.append([x1, y1, x2, y2])
                conf_list.append(conf)

            if not bbox_xyxy_list:
                results.append(_EMPTY)
            else:
                results.append(
                    RawFaceDetection(
                        bbox_xyxy=np.array(bbox_xyxy_list, dtype=np.float32),
                        confidence=np.array(conf_list, dtype=np.float32),
                    )
                )

        return results
