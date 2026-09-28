"""Internal emotion models — dataclasses for core logic, not HTTP."""

from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt


@dataclass
class RawEmotionDetection:
    """Raw recognizer output for a single face crop.

    Contains only what the emotion ONNX model outputs.
    Face-related info (bbox, face_confidence) is added by the session.
    """

    expression_probs: npt.NDArray[np.float32]
    """Shape: (C,) — softmaxed expression probabilities."""

    valence: float | None = None
    arousal: float | None = None


@dataclass
class Emotion:
    """Single classified emotion for one face."""

    emotion: str
    confidence: float
    face_confidence: float
    bbox: list[int]
    valence: float | None = None
    arousal: float | None = None
    probabilities: dict[str, float] | None = None
    """Every class probability keyed by class name; None when not computed."""


@dataclass
class EmotionDetection:
    """Session output: filtered emotions for a single frame."""

    emotions: list[Emotion] = field(default_factory=list)


@dataclass
class EmotionPerceptionSessionConfig:
    confidence_threshold: float = 0.5
    frame_interval: float = 1.0
    label_thresholds: dict[str, float] | None = None
    """Per-label minimum confidence; None uses DEFAULT_LABEL_THRESHOLDS, {} disables gating."""
