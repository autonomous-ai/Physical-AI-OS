"""X3D action recognizer model."""

from pathlib import Path

import numpy as np
import numpy.typing as npt

from core.enums.files import ModelEnum
from core.perception.action.constants import RESOURCES_DIR
from core.perception.action.predictors.base import HumanActionRecognizer
from core.utils.compute import softmax
from core.utils.files import get_default_cdn_url, get_default_model_path


class X3DModel(HumanActionRecognizer):
    """X3D ONNX model for action recognition; the export outputs raw logits, so softmax is applied here."""

    DEFAULT_MODEL_PATH: Path | None = get_default_model_path(ModelEnum.X3D_ONNX)
    DEFAULT_REMOTE_URL: str | None = get_default_cdn_url(ModelEnum.X3D_ONNX)
    DEFAULT_CLASSES_PATH: Path = RESOURCES_DIR / "kinect_classes.txt"
    DEFAULT_WHITELIST_PATH: Path | None = RESOURCES_DIR / "white_list.txt"

    DEFAULT_MAX_FRAMES: int = 16
    DEFAULT_FRAME_SIZE: tuple[int, int] = (256, 256)

    MEAN: npt.NDArray[np.float32] = np.array([114.75, 114.75, 114.75], dtype=np.float32)
    STD: npt.NDArray[np.float32] = np.array([57.38, 57.38, 57.38], dtype=np.float32)

    def _postprocess_probs(self, probs: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
        return softmax(probs, axis=-1)
