"""The facial-emotion label gate, on the device.

It used to live only on the shared perception server, so tuning one label meant
restarting the server for every lamp. It must decide exactly as the server did:
argmax, per-label bar, Neutral fallback carrying Neutral's own probability, then
the confidence floor — and a reading that fails is NO reading, never a Neutral
one, because the occupancy vote counts it against the label.
"""

from hal.drivers.sensing.perceptions.processors.emotion_gating import (
    DEFAULT_LABEL_THRESHOLDS,
    gate_reading,
    load_label_thresholds,
)


def _p(**by_label):
    base = {k: 0.0 for k in ("Neutral", "Happy", "Sad", "Surprise", "Fear", "Disgust", "Anger")}
    base.update(by_label)
    return base


def test_sad_below_its_bar_is_no_reading():
    assert gate_reading(_p(Sad=0.75, Neutral=0.15), DEFAULT_LABEL_THRESHOLDS, 0.5) is None


def test_sad_above_its_bar_stands():
    r = gate_reading(_p(Sad=0.85, Neutral=0.10), DEFAULT_LABEL_THRESHOLDS, 0.5)
    assert (r.label, r.is_fallback) == ("Sad", False)
    assert abs(r.confidence - 0.85) < 1e-9


def test_confident_neutral_passes_through():
    r = gate_reading(_p(Neutral=0.9), DEFAULT_LABEL_THRESHOLDS, 0.5)
    assert r.label == "Neutral"


def test_argmax_below_the_floor_is_no_reading_even_without_a_label_bar():
    assert gate_reading(_p(Neutral=0.45, Sad=0.30), DEFAULT_LABEL_THRESHOLDS, 0.5) is None


def test_label_keys_are_case_insensitive():
    r = gate_reading(_p(Sad=0.75), {"sad": 0.7}, 0.5)
    assert r.label == "Sad"


def test_empty_probabilities_is_no_reading():
    assert gate_reading({}, DEFAULT_LABEL_THRESHOLDS, 0.5) is None


def test_defaults_match_the_server():
    assert DEFAULT_LABEL_THRESHOLDS == {
        "happy": 0.5, "surprise": 0.6, "sad": 0.8,
        "anger": 0.8, "disgust": 0.7, "fear": 0.5,
    }


def test_env_override_replaces_the_map():
    assert load_label_thresholds('{"Sad": 0.9}') == {"sad": 0.9}


def test_unset_env_uses_defaults():
    assert load_label_thresholds("") == DEFAULT_LABEL_THRESHOLDS


def test_malformed_env_falls_back_to_defaults():
    assert load_label_thresholds("{not json") == DEFAULT_LABEL_THRESHOLDS
    assert load_label_thresholds('{"sad": "high"}') == DEFAULT_LABEL_THRESHOLDS
    assert load_label_thresholds("[0.8]") == DEFAULT_LABEL_THRESHOLDS
