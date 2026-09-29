"""POSTER V2 emotion predictor (7-class, RAF-DB); input 224x224 RGB in [0, 1], normalization baked in."""

from pathlib import Path

import numpy as np
import numpy.typing as npt

from core.enums.files import ModelEnum
from core.perception.facial_emotion.constants import RESOURCES_DIR
from core.perception.facial_emotion.predictors.base import EmotionRecognizer
from core.utils.files import get_default_cdn_url, get_default_model_path


class PosterV2Recognizer(EmotionRecognizer):
    """POSTER V2 ONNX emotion predictor."""

    DEFAULT_MODEL_PATH: Path | None = get_default_model_path(ModelEnum.POSTERV2_ONNX)
    DEFAULT_REMOTE_URL: str | None = get_default_cdn_url(ModelEnum.POSTERV2_ONNX)
    DEFAULT_CLASSES_PATH: Path = RESOURCES_DIR / "posterv2_classes.txt"
    DEFAULT_INPUT_SIZE: tuple[int, int] = (224, 224)

    # Identity — normalization is baked into the ONNX export wrapper.
    MEAN: npt.NDArray[np.float32] = np.array([0, 0, 0], dtype=np.float32)
    STD: npt.NDArray[np.float32] = np.array([1, 1, 1], dtype=np.float32)
