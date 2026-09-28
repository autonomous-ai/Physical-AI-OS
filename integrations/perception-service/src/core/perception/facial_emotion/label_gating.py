"""Per-label confidence gating for emotion recognition.

Instead of trusting the raw argmax label unconditionally, each label can
require a minimum confidence. If the winning label does not clear its own
bar, the result falls back to ``Neutral`` (never dropped). Labels absent
from the threshold map pass through as plain argmax.

This keeps the three call sites (HTTP single-face, HTTP frame, WS stream)
consistent and is model-agnostic: thresholds are keyed by lowercased label
name, so the same map applies to PosterV2, EmoNet, and Emo-AffectNet alike.
"""

from typing import NamedTuple

import numpy as np
import numpy.typing as npt

# Default per-label minimum confidence. The argmax label must reach its
# threshold or the result becomes Neutral. Neutral is the fallback target
# and therefore has no threshold of its own. Labels not listed here are
# accepted at argmax with no gating.
DEFAULT_LABEL_THRESHOLDS: dict[str, float] = {
    "happy": 0.5,
    "surprise": 0.6,
    # 0.7 let a bowed head with closed eyes read as Sad on nearly every frame,
    # enough to win the HAL occupancy vote and fire repeatedly. On RAF-DB train
    # (held out from this AffectNet model) 0.8 cuts false Sad from 116 to 44
    # while per-frame Sad recall drops from 0.50 to 0.37.
    "sad": 0.8,
    # 0.6 let a non-frontal face read as Anger on 38% of triggers with a median
    # confidence of 0.62 and never once correctly (device session 2026-09-03,
    # 500 triggers). Observed max on that session was 0.91, so 0.8 keeps the
    # label reachable for a genuinely intense expression while removing the
    # pose-driven noise floor.
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
        LabelResolution with the chosen index/label/confidence. If the argmax
        label is below its threshold, falls back to Neutral (``is_fallback``
        True). If no Neutral label exists, the argmax result is kept.
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
