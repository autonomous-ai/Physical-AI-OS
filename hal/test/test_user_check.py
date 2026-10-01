"""The one "is this my user?" rule set (#545)."""

import math

import numpy as np
import pytest

import hal.app_state as state
import hal.config as config
from hal.drivers.tracking import detection, user_check
from hal.drivers.tracking.user_check import FaceEvidence

NEAR = config.GAZE_BEARING_MIN_FACE_HEIGHT_FRAC + 0.02
FAR = config.GAZE_BEARING_MIN_FACE_HEIGHT_FRAC - 0.02


def test_confirm_needs_a_face():
    assert not user_check.confirms_bearing(None).ok


def test_confirm_rejects_a_far_face():
    v = user_check.confirms_bearing(FaceEvidence(face_h_frac=FAR))
    assert not v.ok and "far" in v.reason


def test_a_near_face_confirms_without_facing():
    """The user talks while looking at their own monitor."""
    assert user_check.confirms_bearing(FaceEvidence(face_h_frac=NEAR)).ok


def test_the_office_co_worker_face_is_not_near():
    """green-lamp 2026-09-30: a side-on co-worker's face was 58 px of a 720 px frame."""
    assert not user_check.near_enough(58 / 720)


def test_the_neighbouring_co_worker_face_is_not_near():
    """green-lamp 2026-09-30 14:31: a co-worker one desk over, 40 px of 360 = 11.1%.

    It confirmed a repoint while the user was away.
    """
    assert not user_check.near_enough(40 / 360)


def test_the_users_smallest_measured_face_is_near():
    """green-lamp 2026-09-30, frames: the user sitting back measured 19.2% at the least."""
    assert user_check.near_enough(0.192)


def test_the_largest_co_worker_face_is_not_near():
    """Same session: the neighbouring co-worker's largest face, 13.6%, confirmed a repoint."""
    assert not user_check.near_enough(0.136)


def test_adopt_rejects_a_near_face_that_is_not_facing():
    ev = FaceEvidence(face_h_frac=NEAR, facing_ratio=0.0, facing_samples=6)
    assert not user_check.adopts_bearing(ev).ok


def test_adopt_accepts_a_centred_face_facing_at_the_bar():
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


def test_adopt_never_accepts_a_far_face():
    ev = FaceEvidence(face_h_frac=FAR, facing_ratio=1.0, facing_samples=6)
    assert not user_check.adopts_bearing(ev).ok


def test_adopt_rejects_a_facing_face_at_the_frame_edge():
    """The office false accept sat at dx=+45%: badly measured and maybe out of reach."""
    ev = FaceEvidence(face_h_frac=NEAR, facing_ratio=1.0, facing_samples=6, dx_frac=0.45)
    v = user_check.adopts_bearing(ev)
    assert not v.ok and "centre" in v.reason


def test_the_bearing_settings_are_their_own():
    """Separate from the wake gate (0.6, 25 deg widened at the edge) and look-aim's 8% floor."""
    assert config.GAZE_BEARING_MIN_FACING_RATIO == pytest.approx(0.4)
    assert config.GAZE_BEARING_MAX_YAW_DEG == pytest.approx(25.0)
    assert config.GAZE_BEARING_MIN_FACE_HEIGHT_FRAC == pytest.approx(0.15)
    assert config.GAZE_MIN_FACING_RATIO == pytest.approx(0.6)
    assert config.LOOK_AIM_MIN_FACE_HEIGHT_FRAC == pytest.approx(0.08)


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


def _observe(monkeypatch, per_frame):
    """per_frame: one face list per grabbed frame (480x640, no downscale)."""
    frames = iter(per_frame)
    current = {}
    monkeypatch.setattr(user_check, "_downscale", lambda f: (f, 1.0))
    monkeypatch.setattr(detection, "detect_faces_with_landmarks",
                        lambda small: current["faces"])

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


def test_a_profile_at_the_frame_edge_does_not_face_even_with_a_wide_wake_cone(monkeypatch):
    """green-lamp's HAL_GAZE_MAX_YAW_DEG=60 x edge widening passed every edge face."""
    monkeypatch.setattr(config, "GAZE_MAX_YAW_DEG", 60.0)
    monkeypatch.setattr(config, "GAZE_EDGE_CONE_SCALE", 1.8)
    edge_profile = _face(560, 100, 100, yaw_deg=70)
    ev = _observe(monkeypatch, [[edge_profile]] * 4)[0].evidence
    assert ev.facing_ratio == 0.0


def test_facing_uses_its_own_limit_not_the_wake_cone(monkeypatch):
    monkeypatch.setattr(config, "GAZE_MAX_YAW_DEG", 60.0)
    turned = _face(270, 100, 100, yaw_deg=40)
    ev = _observe(monkeypatch, [[turned]] * 4)[0].evidence
    assert ev.facing_ratio == 0.0


def test_the_track_carries_its_offset_from_centre(monkeypatch):
    f = _face(560, 100, 60, yaw_deg=5)  # centre x=590 of 640
    ev = _observe(monkeypatch, [[f]] * 3)[0].evidence
    assert ev.dx_frac == pytest.approx((590 - 320) / 640)


def test_a_rejection_logs_the_per_frame_numbers(monkeypatch, caplog):
    side_on = _face(270, 100, 100, yaw_deg=70)
    tracks = _observe(monkeypatch, [[side_on]] * 3)
    with caplog.at_level("INFO"):
        user_check.best_user_face(tracks, "[test]")
    line = next(r.getMessage() for r in caplog.records if "rejected" in r.getMessage())
    assert "yaw=[" in line and "h=[" in line and "dx=" in line


def test_a_fresh_face_id_label_no_longer_lets_a_face_in(monkeypatch):
    """The recognised route is gone: face-ID names who was here, not whose face this is."""
    monkeypatch.setattr(state, "face_user", lambda: ("loc", 0.5))
    side_on = _face(270, 100, 100, yaw_deg=70)
    tracks = _observe(monkeypatch, [[side_on]] * 4)
    assert user_check.best_user_face(tracks, "[test]") is None


def test_a_near_face_off_to_the_side_still_confirms():
    """green-lamp 16:55: the user at 35%, dx +34% (bearing ~20 deg off their seat) was ruled far."""
    assert user_check.confirms_bearing(FaceEvidence(face_h_frac=0.35, dx_frac=0.34)).ok


def test_confirm_accepts_the_user_where_the_repoint_turned():
    """The user's repoint faces measured dx within +/-11%."""
    assert user_check.confirms_bearing(FaceEvidence(face_h_frac=0.30, dx_frac=0.11)).ok


def test_adopt_keeps_its_wider_centre_gate():
    """The sweep's looks overlap only at +/-25%; a tighter gate would leave gaps between them."""
    ev = FaceEvidence(face_h_frac=0.30, facing_ratio=1.0, facing_samples=6, dx_frac=0.20)
    assert user_check.adopts_bearing(ev).ok


def test_near_or_far_is_decided_by_size_alone():
    """Position does not belong in near/far: the repoint centre gate is gone."""
    assert not hasattr(config, "GAZE_REPOINT_MAX_DX_FRAC")
    assert config.BEARING_SAMPLE_MAX_DX_FRAC == pytest.approx(0.25)
