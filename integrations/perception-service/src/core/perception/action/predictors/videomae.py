"""VideoMAE action recognizer model."""

from pathlib import Path

import numpy as np
import numpy.typing as npt

from core.enums.files import ModelEnum
from core.perception.action.constants import RESOURCES_DIR
from core.perception.action.predictors.base import HumanActionRecognizer
from core.utils.compute import softmax
from core.utils.files import get_default_cdn_url, get_default_model_path


class VideoMAEModel(HumanActionRecognizer):
    """VideoMAE ONNX model for action recognition; the export outputs raw logits, so softmax is applied here."""

    DEFAULT_MODEL_PATH: Path | None = get_default_model_path(ModelEnum.VIDEOMAE_ONNX)
    DEFAULT_REMOTE_URL: str | None = get_default_cdn_url(ModelEnum.VIDEOMAE_ONNX)
    DEFAULT_CLASSES_PATH: Path = RESOURCES_DIR / "kinect_classes.txt"
    DEFAULT_WHITELIST_PATH: Path | None = RESOURCES_DIR / "white_list.txt"

    DEFAULT_MAX_FRAMES: int = 16
    DEFAULT_FRAME_SIZE: tuple[int, int] = (224, 224)

    MEAN: npt.NDArray[np.float32] = np.array([123.675, 116.28, 103.53], dtype=np.float32)
    STD: npt.NDArray[np.float32] = np.array([58.395, 57.12, 57.375], dtype=np.float32)

    def _postprocess_probs(self, probs: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
        return softmax(probs, axis=-1)
