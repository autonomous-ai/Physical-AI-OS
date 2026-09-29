"""Emo-AffectNet emotion predictor (7-class, static ResNet-50).

Input: 224x224 RGB crop in [0, 1]; native preprocessing and softmax are baked into the ONNX export.
"""

from pathlib import Path

import numpy as np
import numpy.typing as npt

from core.enums.files import ModelEnum
from core.perception.facial_emotion.constants import RESOURCES_DIR
from core.perception.facial_emotion.predictors.base import EmotionRecognizer
from core.utils.files import get_default_cdn_url, get_default_model_path


class EmoAffectNetRecognizer(EmotionRecognizer):
    """Emo-AffectNet (static ResNet-50) ONNX emotion predictor."""

    DEFAULT_MODEL_PATH: Path | None = get_default_model_path(ModelEnum.EMOAFFECTNET_ONNX)
    DEFAULT_REMOTE_URL: str | None = get_default_cdn_url(ModelEnum.EMOAFFECTNET_ONNX)
    DEFAULT_CLASSES_PATH: Path = RESOURCES_DIR / "emoaffectnet_classes.txt"
    DEFAULT_INPUT_SIZE: tuple[int, int] = (224, 224)

    # Identity: native preprocessing is baked into the ONNX export.
    MEAN: npt.NDArray[np.float32] = np.array([0, 0, 0], dtype=np.float32)
    STD: npt.NDArray[np.float32] = np.array([1, 1, 1], dtype=np.float32)
