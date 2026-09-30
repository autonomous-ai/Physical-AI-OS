"""The one "is this my user?" rule set (#545)."""

import pytest

import hal.app_state as state
import hal.config as config
from hal.drivers.tracking import user_check
from hal.drivers.tracking.user_check import FaceEvidence

NEAR = config.LOOK_AIM_MIN_FACE_HEIGHT_FRAC + 0.02
FAR = config.LOOK_AIM_MIN_FACE_HEIGHT_FRAC - 0.02


@pytest.mark.parametrize("label, known", [
    ("", False), ("unknown", False), ("stranger_16", False), ("loc", True),
])
def test_only_a_friend_label_is_recognised(label, known):
    assert user_check.is_recognised(label) is known


def test_a_fresh_friend_label_is_used(monkeypatch):
    monkeypatch.setattr(state, "face_user", lambda: ("loc", 1.0))
    assert user_check.fresh_identity() == "loc"


def test_a_stale_friend_label_is_not_fresh(monkeypatch):
    """The owner seen a minute ago says nothing about the face in frame now."""
    monkeypatch.setattr(state, "face_user", lambda: ("loc", 60.0))
    assert user_check.fresh_identity() == ""


def test_a_stranger_label_is_never_an_identity(monkeypatch):
    monkeypatch.setattr(state, "face_user", lambda: ("unknown", 0.5))
    assert user_check.fresh_identity() == ""


def test_identity_lookup_failure_is_no_identity(monkeypatch):
    def boom():
        raise RuntimeError("no face-ID")
    monkeypatch.setattr(state, "face_user", boom)
    assert user_check.fresh_identity() == ""


def test_confirm_needs_a_face():
    assert not user_check.confirms_bearing(None).ok


def test_confirm_rejects_a_far_face():
    v = user_check.confirms_bearing(FaceEvidence(face_h_frac=FAR))
    assert not v.ok and "far" in v.reason


def test_a_near_face_confirms_without_facing_or_identity():
    """The user talks while looking at their own monitor."""
    assert user_check.confirms_bearing(FaceEvidence(face_h_frac=NEAR)).ok


def test_adopt_rejects_a_near_face_that_is_neither_known_nor_facing():
    ev = FaceEvidence(face_h_frac=NEAR, facing_ratio=0.0, facing_samples=6)
    assert not user_check.adopts_bearing(ev).ok


def test_adopt_accepts_a_recognised_face_without_facing():
    ev = FaceEvidence(face_h_frac=NEAR, identity="loc")
    assert user_check.adopts_bearing(ev).ok


def test_adopt_accepts_a_face_facing_at_the_bar():
    ev = FaceEvidence(face_h_frac=NEAR,
                      facing_ratio=config.GAZE_BEARING_MIN_FACING_RATIO,
                      facing_samples=config.GAZE_MIN_SAMPLES)
    assert user_check.adopts_bearing(ev).ok


def test_adopt_rejects_facing_below_the_bar():
    ev = FaceEvidence(face_h_frac=NEAR,
                      facing_ratio=config.GAZE_BEARING_MIN_FACING_RATIO - 0.01,
                      facing_samples=6)
    assert not user_check.adopts_bearing(ev).ok


def test_adopt_rejects_facing_measured_on_too_few_samples():
    ev = FaceEvidence(face_h_frac=NEAR, facing_ratio=1.0,
                      facing_samples=config.GAZE_MIN_SAMPLES - 1)
    assert not user_check.adopts_bearing(ev).ok


def test_adopt_never_accepts_a_far_face_even_if_recognised():
    ev = FaceEvidence(face_h_frac=FAR, identity="loc", facing_ratio=1.0, facing_samples=6)
    assert not user_check.adopts_bearing(ev).ok


def test_the_bearing_bar_is_lower_than_the_wake_bar():
    """Separate settings: 0.40 for bearings, the wake gate stays at 0.6."""
    assert config.GAZE_BEARING_MIN_FACING_RATIO == pytest.approx(0.4)
    assert config.GAZE_MIN_FACING_RATIO == pytest.approx(0.6)
