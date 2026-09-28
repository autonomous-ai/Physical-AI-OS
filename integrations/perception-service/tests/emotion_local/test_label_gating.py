"""Per-label gating: what a label must score before it is allowed to stand.

The gate is the only thing between a noisy FER read and a sensing event, and it
had no coverage. Anger is the label that matters. At 0.6 it was the argmax on
38% of triggers across a 500-trigger device session and never once correct — a
face turned toward a monitor reads as Anger to an AffectNet-trained model. The
observed maximum on that session was 0.91, so 0.8 removes the noise floor while
leaving the label reachable for a genuinely intense expression.

The second rule here is the one callers get wrong: a failed gate does not drop
the reading, it returns **Neutral carrying Neutral's own probability**, which is
typically low. So `confidence` on a returned Neutral is not a quality score, and
an empty HTTP response means "nothing cleared its bar", not "no face was found".
"""

import numpy as np

from core.perception.facial_emotion.label_gating import (
    DEFAULT_LABEL_THRESHOLDS,
    resolve_label,
)

# resources/emoaffectnet_classes.txt, in order.
CLASSES = ["Neutral", "Happy", "Sad", "Surprise", "Fear", "Disgust", "Anger"]


def _probs(**by_label: float) -> np.ndarray:
    p = np.zeros(len(CLASSES), dtype=np.float32)
    for name, value in by_label.items():
        p[CLASSES.index(name)] = value
    return p


def test_anger_just_under_its_gate_becomes_neutral():
    r = resolve_label(_probs(Anger=0.79, Neutral=0.11), CLASSES)
    assert r.label == "Neutral"
    assert r.is_fallback is True


def test_the_fallback_reports_neutrals_own_probability_not_the_losers():
    """0.11, not 0.79 — the reason `confidence` cannot be read as certainty."""
    r = resolve_label(_probs(Anger=0.79, Neutral=0.11), CLASSES)
    assert abs(r.confidence - 0.11) < 1e-6


def test_anger_over_its_gate_stands():
    r = resolve_label(_probs(Anger=0.81, Neutral=0.05), CLASSES)
    assert r.label == "Anger"
    assert r.is_fallback is False
    assert abs(r.confidence - 0.81) < 1e-6


def test_the_anger_gate_is_the_value_the_device_was_tuned_against():
    """Guards an accidental revert to 0.6, which is a 38%-false-positive setting."""
    assert DEFAULT_LABEL_THRESHOLDS["anger"] == 0.8


def test_the_sad_gate_rejects_a_bowed_head_reading():
    """Guards a revert to 0.7, where a bowed head with closed eyes spammed Sad."""
    assert DEFAULT_LABEL_THRESHOLDS["sad"] == 0.8
    r = resolve_label(_probs(Sad=0.75, Neutral=0.15), CLASSES)
    assert r.label == "Neutral"
    assert r.is_fallback is True


def test_raising_anger_and_sad_left_the_other_labels_alone():
    assert DEFAULT_LABEL_THRESHOLDS["happy"] == 0.5
    assert DEFAULT_LABEL_THRESHOLDS["surprise"] == 0.6
    assert DEFAULT_LABEL_THRESHOLDS["disgust"] == 0.7
    assert DEFAULT_LABEL_THRESHOLDS["fear"] == 0.5


def test_happy_still_passes_at_its_own_unchanged_bar():
    r = resolve_label(_probs(Happy=0.55, Neutral=0.20), CLASSES)
    assert r.label == "Happy"
    assert r.is_fallback is False


def test_a_label_with_no_gate_passes_at_argmax():
    """Neutral is the fallback target and has no bar of its own."""
    r = resolve_label(_probs(Neutral=0.24, Anger=0.20), CLASSES)
    assert r.label == "Neutral"
    assert r.is_fallback is False
