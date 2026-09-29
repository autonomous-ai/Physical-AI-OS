"""Per-label confidence gating for emotion recognition (model-agnostic, keyed by lowercased label)."""

from typing import NamedTuple

import numpy as np
import numpy.typing as npt

# Argmax below its threshold falls back to Neutral; unlisted labels are not gated.
DEFAULT_LABEL_THRESHOLDS: dict[str, float] = {
    "happy": 0.5,
    "surprise": 0.6,
    # 0.7 fired on bowed heads; on RAF-DB 0.8 cuts false Sad 116 -> 44 (recall 0.50 -> 0.37).
    "sad": 0.8,
    # 0.6 misread non-frontal faces as Anger (median 0.62); 0.8 removes that noise floor.
    "anger": 0.8,
    "disgust": 0.7,
    "fear": 0.5,
}

NEUTRAL_LABEL: str = "neutral"


class LabelResolution(NamedTuple):
    """Outcome of per-label gating for a single face."""

    index: int
    label: str
    confidence: float
    is_fallback: bool
    """True when the argmax label failed its threshold and was replaced by Neutral."""


def _find_neutral_index(class_names: list[str]) -> int | None:
    for i, name in enumerate(class_names):
        if name.strip().lower() == NEUTRAL_LABEL:
            return i
    return None


def resolve_label(
    probs: npt.NDArray[np.float32],
    class_names: list[str],
    thresholds: dict[str, float] | None = None,
) -> LabelResolution:
    """Pick the emotion label, gating the argmax winner by per-label threshold.

    Args:
        probs: Softmaxed expression probabilities, shape (C,).
        class_names: Label for each probability index.
        thresholds: Per-label minimum confidence keyed by lowercased label.
            ``None`` uses ``DEFAULT_LABEL_THRESHOLDS``; pass ``{}`` to disable
            gating entirely (pure argmax).

    Returns:
        LabelResolution; Neutral with ``is_fallback`` when argmax misses its threshold.
    """
    if thresholds is None:
        thresholds = DEFAULT_LABEL_THRESHOLDS

    idx: int = int(np.argmax(probs))
    label: str = class_names[idx]
    confidence: float = float(probs[idx])

    threshold: float | None = thresholds.get(label.strip().lower())
    if threshold is None or confidence >= threshold:
        return LabelResolution(idx, label, confidence, False)

    neutral_idx: int | None = _find_neutral_index(class_names)
    if neutral_idx is None:
        return LabelResolution(idx, label, confidence, False)

    return LabelResolution(
        neutral_idx, class_names[neutral_idx], float(probs[neutral_idx]), True
    )
