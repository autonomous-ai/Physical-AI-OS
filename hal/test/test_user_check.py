"""The one "is this my user?" rule set (#545)."""

import math

import numpy as np
import pytest

import hal.app_state as state
import hal.config as config
from hal.drivers.tracking import detection, user_check
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



def _yunet_rows(*faces):
    """faces: (x, y, w, h). Level eyes, centred nose: YuNet's 15-column row."""
    rows = []
    for x, y, w, h in faces:
        rex, lex, ey = x + w * 0.3, x + w * 0.7, y + h * 0.4
        rows.append([x, y, w, h, rex, ey, lex, ey, (rex + lex) / 2, y + h * 0.6,
                     x + w * 0.35, y + h * 0.8, x + w * 0.65, y + h * 0.8, 0.9])
    return np.array(rows, dtype=np.float32)


class _FakeYunet:
    def __init__(self, rows):
        self.rows = rows

    def setInputSize(self, size):
        pass

    def detect(self, frame):
        return 1, self.rows


def test_every_face_is_returned_largest_first(monkeypatch):
    rows = _yunet_rows((10, 10, 30, 30), (200, 50, 80, 80))
    monkeypatch.setattr(detection, "_get_yunet", lambda: _FakeYunet(rows))
    faces = detection.detect_faces_with_landmarks(np.zeros((480, 640, 3), np.uint8))
    assert [f[0][3] for f in faces] == [80, 30]
    assert len(faces[0][1]) == 10


def test_no_detector_means_no_faces(monkeypatch):
    monkeypatch.setattr(detection, "_get_yunet", lambda: None)
    assert detection.detect_faces_with_landmarks(np.zeros((480, 640, 3), np.uint8)) == []


def _face(x, y, h, yaw_deg):
    """((x, y, w, h), landmarks) whose head_yaw_deg is ~yaw_deg."""
    w = h
    rex, lex, ey = x + w * 0.3, x + w * 0.7, y + h * 0.4
    half = (lex - rex) / 2.0
    nose_x = (rex + lex) / 2.0 + half * math.sin(math.radians(yaw_deg))
    lm = (rex, ey, lex, ey, nose_x, y + h * 0.6, x + w * 0.35, y + h * 0.8,
          x + w * 0.65, y + h * 0.8)
    return (x, y, w, h), lm


def _observe(monkeypatch, per_frame, identity=""):
    """per_frame: one face list per grabbed frame (480x640, no downscale)."""
    frames = iter(per_frame)
    current = {}
    monkeypatch.setattr(user_check, "_downscale", lambda f: (f, 1.0))
    monkeypatch.setattr(detection, "detect_faces_with_landmarks",
                        lambda small: current["faces"])
    monkeypatch.setattr(user_check, "fresh_identity", lambda: identity)

    def grab():
        current["faces"] = next(frames)
        return np.zeros((480, 640, 3), np.uint8)

    return user_check.observe_faces(grab, frames=len(per_frame), interval_s=0.0,
                                    sleep=lambda s: None)


def test_an_empty_first_frame_stops_the_observation(monkeypatch):
    """No dwell when nobody is there: a sweep must not pay 1.5 s per empty look."""
    grabbed = []
    monkeypatch.setattr(user_check, "_downscale", lambda f: (f, 1.0))
    monkeypatch.setattr(detection, "detect_faces_with_landmarks", lambda small: [])

    def grab():
        grabbed.append(1)
        return np.zeros((480, 640, 3), np.uint8)

    assert user_check.observe_faces(grab, frames=6, interval_s=0.0,
                                    sleep=lambda s: None) == []
    assert len(grabbed) == 1


def test_one_face_seen_over_frames_becomes_one_track(monkeypatch):
    f = _face(300, 100, 100, yaw_deg=5)
    tracks = _observe(monkeypatch, [[f]] * 4)
    assert len(tracks) == 1
    ev = tracks[0].evidence
    assert ev.facing_samples == 4 and ev.facing_ratio == 1.0
    assert ev.face_h_frac == pytest.approx(100 / 480)


def test_a_side_on_face_does_not_face(monkeypatch):
    f = _face(300, 100, 100, yaw_deg=70)
    ev = _observe(monkeypatch, [[f]] * 4)[0].evidence
    assert ev.facing_ratio == 0.0


def test_tracks_rank_facing_before_size(monkeypatch):
    """A bigger side-on co-worker loses to a smaller user who faces the lamp."""
    coworker = _face(20, 100, 140, yaw_deg=70)
    user = _face(420, 120, 90, yaw_deg=5)
    tracks = _observe(monkeypatch, [[coworker, user]] * 4)
    assert tracks[0].box[0] == 420
    assert user_check.best_user_face(tracks, "[test]").box[0] == 420


def test_nobody_qualifies_returns_none(monkeypatch):
    coworker = _face(20, 100, 140, yaw_deg=70)
    tracks = _observe(monkeypatch, [[coworker]] * 4)
    assert user_check.best_user_face(tracks, "[test]") is None


def test_a_fresh_identity_lets_a_near_face_qualify_without_facing(monkeypatch):
    side_on = _face(300, 100, 100, yaw_deg=70)
    tracks = _observe(monkeypatch, [[side_on]] * 4, identity="loc")
    assert user_check.best_user_face(tracks, "[test]") is not None
