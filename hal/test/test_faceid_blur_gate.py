"""The blur gate drops frames too smeared to carry an identity."""

import cv2
import numpy as np

import hal.config as config
from hal.drivers.sensing.perceptions.processors.faceid.recognizer import (
    FaceRecognizer,
)


def _detailed_crop(size: int = 112) -> np.ndarray:
    """A 112x112 with plenty of high-frequency detail, standing in for a face."""
    rng = np.random.default_rng(0)
    return rng.integers(0, 255, (size, size, 3), dtype=np.uint8)


def test_sharpness_falls_as_blur_rises():
    sharp = _detailed_crop()
    scores = [
        FaceRecognizer._sharpness(cv2.GaussianBlur(sharp, (0, 0), s))
        if s
        else FaceRecognizer._sharpness(sharp)
        for s in (0, 1, 2, 4)
    ]
    assert scores == sorted(scores, reverse=True), scores


def test_blurred_crop_falls_below_the_shipped_default():
    """A heavily smeared crop must land under the gate, a detailed one over it."""
    sharp = _detailed_crop()
    smeared = cv2.GaussianBlur(sharp, (0, 0), 4)
    assert FaceRecognizer._sharpness(sharp) > config.FACE_MIN_SHARPNESS
    assert FaceRecognizer._sharpness(smeared) < config.FACE_MIN_SHARPNESS


def test_sharpness_is_scale_sensitive_hence_measured_on_the_aligned_crop():
    """Why the gate must use the aligned 112x112 and never the detector crop."""
    sharp = _detailed_crop(112)
    upscaled = cv2.resize(sharp, (224, 224), interpolation=cv2.INTER_LINEAR)
    assert FaceRecognizer._sharpness(upscaled) != FaceRecognizer._sharpness(sharp)


def test_default_is_a_positive_floor():
    """A zero/negative floor would disable the gate silently."""
    assert config.FACE_MIN_SHARPNESS > 0
