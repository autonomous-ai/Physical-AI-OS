"""Per-label confidence gate for facial emotion, applied on the device.

Mirrors integrations/perception-service label_gating.resolve_label plus the
emotion-recognize route's threshold drop, so a reading is judged identically
whichever side gates it. HAL asks the server for raw probabilities and gates
here, which makes each label's bar a per-device HAL setting instead of a change
to the shared server.

The outcome that matters: a reading that fails is None — "no confirmed
reading" — never a Neutral reading. The occupancy vote counts no-readings
against a label, so turning them into Neutral would weaken it.
"""

import json
import logging
from typing import NamedTuple

logger = logging.getLogger(__name__)

# Keyed by lowercased label. Neutral is the fallback target and has no bar;
# labels not listed are accepted at argmax. Sad and Anger sit high because an
# AffectNet-trained model reads a bowed head (Sad) or a face turned toward a
# monitor (Anger) confidently wrong.
DEFAULT_LABEL_THRESHOLDS: dict[str, float] = {
    "happy": 0.5,
    "surprise": 0.6,
    "sad": 0.8,
    "anger": 0.8,
    "disgust": 0.7,
    "fear": 0.5,
}

NEUTRAL_LABEL = "neutral"


class GatedReading(NamedTuple):
    label: str
    confidence: float
    is_fallback: bool
    """True when the argmax failed its bar and Neutral stood in for it."""


def gate_reading(
    probabilities: dict[str, float],
    thresholds: dict[str, float],
    min_confidence: float,
) -> GatedReading | None:
    """Judge one face's class probabilities. None means no confirmed reading."""
    if not probabilities:
        return None

    label = max(probabilities, key=probabilities.__getitem__)
    confidence = float(probabilities[label])
    is_fallback = False

    threshold = thresholds.get(label.strip().lower())
    if threshold is not None and confidence < threshold:
        neutral = next(
            (k for k in probabilities if k.strip().lower() == NEUTRAL_LABEL), None
        )
        if neutral is not None:
            label, confidence, is_fallback = neutral, float(probabilities[neutral]), True

    # Same floor the server route applies: below it the server answered with
    # no detection at all.
    if confidence < min_confidence:
        return None
    return GatedReading(label, confidence, is_fallback)


def load_label_thresholds(raw_json: str) -> dict[str, float]:
    """Parse HAL_EMOTION_LABEL_THRESHOLDS; defaults when unset or malformed.

    A set value replaces the whole map, as FER__LABEL_THRESHOLDS does on the
    server. A bad value must not take sensing down, so it logs and falls back.
    """
    if not raw_json.strip():
        return dict(DEFAULT_LABEL_THRESHOLDS)
    try:
        parsed = json.loads(raw_json)
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
        return {str(k).strip().lower(): float(v) for k, v in parsed.items()}
    except (ValueError, TypeError) as e:
        logger.warning(
            "[activity.emotion] invalid HAL_EMOTION_LABEL_THRESHOLDS %r (%s) — using defaults",
            raw_json,
            e,
        )
        return dict(DEFAULT_LABEL_THRESHOLDS)
