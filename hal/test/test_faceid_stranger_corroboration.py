"""An unknown face must persist for a few ticks before it earns an identity."""

import time

import numpy as np
import pytest

import hal.config as config
from hal.drivers.sensing.perceptions.processors.faceid.recognizer import (
    FaceRecognizer,
)


@pytest.fixture
def rec() -> FaceRecognizer:
    """Recogniser with no models started, for corroboration bookkeeping only."""
    return FaceRecognizer()


def _emb(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=512).astype(np.float32)
    return v / np.linalg.norm(v)


def test_a_single_sighting_does_not_reach_the_threshold(rec):
    assert rec._corroborate_stranger(_emb(1)) < config.FACE_STRANGER_MIN_TICKS


def test_the_same_face_seen_again_corroborates(rec):
    face = _emb(1)
    assert rec._corroborate_stranger(face) == 1
    assert rec._corroborate_stranger(face) == 2
    assert 2 >= config.FACE_STRANGER_MIN_TICKS


def test_two_different_faces_do_not_corroborate_each_other(rec):
    """The transient case: two unrelated bad frames in a row must not add up."""
    assert rec._corroborate_stranger(_emb(1)) == 1
    assert rec._corroborate_stranger(_emb(99)) == 1


def test_two_unknown_people_each_accumulate_independently(rec):
    """Two pending strangers are tracked separately so both can mint."""
    a, b = _emb(1), _emb(99)
    assert (rec._corroborate_stranger(a), rec._corroborate_stranger(b)) == (1, 1)
    assert (rec._corroborate_stranger(a), rec._corroborate_stranger(b)) == (2, 2)


def test_a_stale_candidate_is_forgotten(rec, monkeypatch):
    """'In a row' must mean consecutive ticks, not twice in the same hour."""
    face = _emb(1)
    assert rec._corroborate_stranger(face) == 1
    real = time.time
    monkeypatch.setattr(
        time, "time", lambda: real() + config.FACE_STRANGER_CORROBORATION_S + 1
    )
    assert rec._corroborate_stranger(face) == 1


def test_corroboration_window_spans_several_sensing_ticks():
    """A window shorter than the tick interval could never corroborate at all."""
    assert config.FACE_STRANGER_CORROBORATION_S > 2 * 2.0
    assert config.FACE_STRANGER_MIN_TICKS >= 1
