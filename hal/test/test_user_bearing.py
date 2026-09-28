"""Focused tests for the remembered user bearing."""

import json
import os
import tempfile
from unittest import mock

import hal.config as config
from hal.drivers.tracking import user_bearing as ub


def _tmp_path(tmpdir):
    return os.path.join(tmpdir, "user_bearing.json")


def _with_path(tmpdir):
    return mock.patch.object(config, "USER_BEARING_PATH", _tmp_path(tmpdir), create=True)


def test_first_sighting_is_taken_verbatim():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        assert ub.record_sighting(-42.0) is True
        est = ub.read_estimate()
        assert est is not None
        assert est.bearing_deg == -42.0
        assert est.samples == 1


def test_repeated_sightings_converge_on_the_real_position():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        ub.record_sighting(0.0, now=t)
        for i in range(1, 12):
            ub.record_sighting(30.0, now=t + i * 60.0)
        est = ub.read_estimate(now=t + 12 * 60.0)
        assert 28.0 < est.bearing_deg <= 30.0, est.bearing_deg


def test_one_stray_sample_cannot_hijack_the_estimate():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        for i in range(10):
            ub.record_sighting(20.0, now=t + i * 60.0)
        ub.record_sighting(-120.0, now=t + 11 * 60.0)
        est = ub.read_estimate(now=t + 12 * 60.0)
        assert est.bearing_deg > 0.0, "a single outlier must not flip the sign"


def test_rate_limit_drops_rapid_sightings():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        assert ub.record_sighting(10.0, now=t) is True
        assert ub.record_sighting(10.0, now=t + 1.0) is False
        assert ub.read_estimate(now=t + 1.0).samples == 1


def test_confidence_does_not_decay_with_age():
    """Confidence measures how well the estimate is LEARNED, not how recent."""
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        for i in range(ub.CONFIDENCE_FULL_SAMPLES):
            ub.record_sighting(15.0, now=t + i * 60.0)
        fresh = ub.read_estimate(now=t + 8 * 60.0).confidence
        stale = ub.read_estimate(now=t + 8 * 60.0 + 48 * 3600).confidence
        assert fresh > 0.9
        assert stale == fresh, "age must not move confidence"
        assert ub.read_estimate(now=t + 8 * 60.0 + 48 * 3600).age_s > 47 * 3600


def test_confidence_still_grows_with_sightings():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        ub.record_sighting(15.0, now=t)
        one = ub.read_estimate(now=t).confidence
        for i in range(1, ub.CONFIDENCE_FULL_SAMPLES):
            ub.record_sighting(15.0, now=t + i * 60.0)
        full = ub.read_estimate(now=t + 8 * 60.0).confidence
        assert 0.0 < one < full
        assert full > 0.9


def test_no_estimate_reads_as_none_not_zero():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        assert ub.read_estimate() is None


def test_clear_forgets_the_estimate():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        ub.record_sighting(50.0)
        assert ub.clear() is True
        assert ub.read_estimate() is None
        assert ub.clear() is True


def test_corrupt_file_is_ignored_not_fatal():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        with open(_tmp_path(d), "w") as f:
            f.write("{ this is not json")
        assert ub.read_estimate() is None
        assert ub.record_sighting(5.0) is True


def test_wrong_schema_version_is_ignored():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        with open(_tmp_path(d), "w") as f:
            json.dump({"version": 999, "bearing_deg": 77.0, "samples": 50}, f)
        assert ub.read_estimate() is None


def test_write_is_atomic_no_partial_file_left_behind():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        ub.record_sighting(12.0)
        leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
        assert leftovers == [], f"temp files left behind: {leftovers}"


def test_a_sustained_move_is_accepted_as_relocation():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        for i in range(10):
            ub.record_sighting(20.0, now=t + i * 60.0)
        for i in range(10, 10 + ub.OUTLIER_STREAK):
            ub.record_sighting(-100.0, now=t + i * 60.0)
        est = ub.read_estimate(now=t + 20 * 60.0)
        assert est.bearing_deg < -50.0, f"relocation not accepted: {est.bearing_deg}"


def test_early_sightings_are_not_treated_as_outliers():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        ub.record_sighting(0.0, now=t)
        ub.record_sighting(90.0, now=t + 60.0)
        assert ub.read_estimate(now=t + 120.0).bearing_deg > 10.0


def test_repeated_failed_predictions_drop_the_estimate():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        ub.record_sighting(30.0)
        for _ in range(ub.PREDICTION_MISS_LIMIT - 1):
            assert ub.record_prediction(hit=False) is False
            assert ub.read_estimate() is not None
        assert ub.record_prediction(hit=False) is True
        assert ub.read_estimate() is None, "estimate should be dropped"


def test_a_single_miss_does_not_drop_the_estimate():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        ub.record_sighting(30.0)
        ub.record_prediction(hit=False)
        assert ub.read_estimate() is not None


def test_a_hit_clears_the_miss_streak():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        ub.record_sighting(30.0)
        for _ in range(ub.PREDICTION_MISS_LIMIT - 1):
            ub.record_prediction(hit=False)
        ub.record_prediction(hit=True)
        # Streak reset, so the limit must start over rather than trip immediately.
        for _ in range(ub.PREDICTION_MISS_LIMIT - 1):
            assert ub.record_prediction(hit=False) is False
        assert ub.read_estimate() is not None


def test_scoring_with_no_estimate_is_harmless():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        assert ub.record_prediction(hit=False) is False
        assert ub.record_prediction(hit=True) is False


def test_misses_spread_far_apart_do_not_accumulate():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        ub.record_sighting(30.0, now=t)
        for i in range(6):
            far_apart = t + (i + 1) * (ub.MISS_STREAK_WINDOW_S + 60.0)
            assert ub.record_prediction(hit=False, now=far_apart) is False
        assert ub.read_estimate(now=t) is not None, "scattered misses must not drop it"


def test_clustered_misses_still_drop_the_estimate():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        ub.record_sighting(30.0, now=t)
        dropped = False
        for i in range(ub.PREDICTION_MISS_LIMIT):
            dropped = ub.record_prediction(hit=False, now=t + i * 300.0)
        assert dropped is True
        assert ub.read_estimate(now=t) is None


def test_a_small_lamp_move_self_corrects_without_being_detected():
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        t = 1_000_000.0
        for i in range(10):
            ub.record_sighting(30.0, now=t + i * 60.0)
        assert abs(ub.read_estimate(now=t).bearing_deg - 30.0) < 1.0

        # Under OUTLIER_DEG, so it is folded in at full weight.
        shifted = 50.0
        assert abs(shifted - 30.0) < ub.OUTLIER_DEG
        for i in range(10, 22):
            ub.record_sighting(shifted, now=t + i * 60.0)

        est = ub.read_estimate(now=t + 22 * 60.0)
        assert abs(est.bearing_deg - shifted) < 2.0, (
            f"estimate should have followed the lamp to {shifted}, got {est.bearing_deg}"
        )


def test_sighting_stores_the_whole_posture(tmp_path, monkeypatch):
    """The bearing stores a full posture, not just yaw."""
    monkeypatch.setattr(ub.config, "USER_BEARING_PATH", str(tmp_path / "b.json"), raising=False)
    pose = {"base_yaw.pos": 20.0, "base_pitch.pos": 5.0, "elbow_pitch.pos": 10.0}
    assert ub.record_sighting(20.0, pose=pose) is True
    est = ub.read_estimate()
    assert est.pose["base_pitch.pos"] == 5.0
    assert est.pose["elbow_pitch.pos"] == 10.0


def test_bearing_stays_consistent_with_the_pose_yaw(tmp_path, monkeypatch):
    """The scalar bearing stays consistent with the posture's yaw."""
    monkeypatch.setattr(ub.config, "USER_BEARING_PATH", str(tmp_path / "b.json"), raising=False)
    ub.record_sighting(20.0, pose={"base_yaw.pos": 20.0, "base_pitch.pos": 5.0})
    ub.record_sighting(30.0, pose={"base_yaw.pos": 30.0, "base_pitch.pos": 9.0},
                       now=__import__("time").time() + 60.0)
    est = ub.read_estimate()
    assert est.bearing_deg == est.pose["base_yaw.pos"]


def test_pose_joints_are_smoothed_like_the_bearing(tmp_path, monkeypatch):
    """Each joint is smoothed with its own EMA."""
    import time as _t

    monkeypatch.setattr(ub.config, "USER_BEARING_PATH", str(tmp_path / "b.json"), raising=False)
    ub.record_sighting(20.0, pose={"base_yaw.pos": 20.0, "base_pitch.pos": 0.0})
    ub.record_sighting(20.0, pose={"base_yaw.pos": 20.0, "base_pitch.pos": 20.0},
                       now=_t.time() + 60.0)
    pitch = ub.read_estimate().pose["base_pitch.pos"]
    assert 0.0 < pitch < 20.0, f"expected a smoothed pitch, got {pitch}"


def test_v1_file_is_no_longer_migrated(tmp_path, monkeypatch):
    """A v1 file is no longer migrated."""
    path = tmp_path / "b.json"
    path.write_text(json.dumps({
        "version": 1, "bearing_deg": 25.709, "confidence": 0.25,
        "samples": 2, "outlier_streak": 0, "updated": __import__("time").time(),
    }))
    monkeypatch.setattr(ub.config, "USER_BEARING_PATH", str(path), raising=False)
    assert ub.read_estimate() is None


def test_relocation_replaces_the_posture_rather_than_averaging_it(tmp_path, monkeypatch):
    """The old shape describes the old place; blending them would aim between."""
    import time as _t

    monkeypatch.setattr(ub.config, "USER_BEARING_PATH", str(tmp_path / "b.json"), raising=False)
    t = _t.time()
    for i in range(6):
        ub.record_sighting(0.0, pose={"base_yaw.pos": 0.0, "base_pitch.pos": 0.0},
                           now=t + i * 60.0)
    t2 = t + 600.0
    for i in range(ub.OUTLIER_STREAK):
        ub.record_sighting(80.0, pose={"base_yaw.pos": 80.0, "base_pitch.pos": 30.0},
                           now=t2 + i * 60.0)
    est = ub.read_estimate()
    assert est.pose["base_pitch.pos"] == 30.0, est.pose


def test_a_sighting_without_a_pose_still_moves_the_bearing(tmp_path, monkeypatch):
    """A sighting without a pose still updates the yaw bearing."""
    import time as _t

    monkeypatch.setattr(ub.config, "USER_BEARING_PATH", str(tmp_path / "b.json"), raising=False)
    ub.record_sighting(0.0, pose={"base_yaw.pos": 0.0, "base_pitch.pos": 5.0})
    ub.record_sighting(40.0, now=_t.time() + 60.0)
    est = ub.read_estimate()
    assert est.bearing_deg > 0.0, "the pose-less sighting was ignored"
    assert est.pose["base_pitch.pos"] == 5.0, "the known posture was lost"
    assert est.pose["base_yaw.pos"] == est.bearing_deg


def _seed(d, payload):
    with open(_tmp_path(d), "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _fp(value):
    return mock.patch.object(ub, "_calibration_fingerprint", lambda: value)


def test_a_v2_estimate_is_dropped_because_its_calibration_is_unknown():
    """Every angle is degrees ON A CALIBRATION, and v2 never recorded which."""
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        _seed(d, {"version": 2, "bearing_deg": 42.0, "pose": {"base_yaw.pos": 42.0},
                  "confidence": 1.0, "samples": 20, "updated": 1_000_000.0})
        assert ub.read_estimate(now=1_000_001.0) is None


def test_a_v1_estimate_is_dropped_too_not_migrated():
    """v1 yaw is not kept after recalibration."""
    with tempfile.TemporaryDirectory() as d, _with_path(d):
        _seed(d, {"version": 1, "bearing_deg": 42.0, "samples": 20,
                  "updated": 1_000_000.0})
        assert ub.read_estimate(now=1_000_001.0) is None


def test_a_pose_from_a_different_calibration_is_refused():
    with tempfile.TemporaryDirectory() as d, _with_path(d), _fp("beef1234"):
        _seed(d, {"version": 3, "calibration": "0000dead", "bearing_deg": 42.0,
                  "pose": {"base_yaw.pos": 42.0}, "confidence": 1.0,
                  "samples": 20, "updated": 1_000_000.0})
        assert ub.read_estimate(now=1_000_001.0) is None


def test_a_pose_from_the_same_calibration_is_kept():
    with tempfile.TemporaryDirectory() as d, _with_path(d), _fp("beef1234"):
        _seed(d, {"version": 3, "calibration": "beef1234", "bearing_deg": 42.0,
                  "pose": {"base_yaw.pos": 42.0}, "confidence": 1.0,
                  "samples": 20, "updated": 1_000_000.0})
        est = ub.read_estimate(now=1_000_001.0)
        assert est is not None and est.bearing_deg == 42.0


def test_an_unreadable_calibration_does_not_wipe_the_estimate():
    """Missing or unreadable calibration does not wipe bearings."""
    with tempfile.TemporaryDirectory() as d, _with_path(d), _fp(None):
        _seed(d, {"version": 3, "calibration": "0000dead", "bearing_deg": 42.0,
                  "pose": {"base_yaw.pos": 42.0}, "confidence": 1.0,
                  "samples": 20, "updated": 1_000_000.0})
        est = ub.read_estimate(now=1_000_001.0)
        assert est is not None and est.bearing_deg == 42.0


def test_a_sighting_stamps_the_live_calibration():
    with tempfile.TemporaryDirectory() as d, _with_path(d), _fp("beef1234"):
        ub.record_sighting(10.0, pose={"base_yaw.pos": 10.0})
        with open(_tmp_path(d), encoding="utf-8") as f:
            assert json.load(f)["calibration"] == "beef1234"


def test_the_fingerprint_follows_content_not_timestamp():
    """An OTA rewrites the calibration's mtime without changing an offset."""
    with tempfile.TemporaryDirectory() as d:
        cal = os.path.join(d, "hal.json")
        with open(cal, "w", encoding="utf-8") as f:
            f.write('{"base_yaw": {"homing_offset": 0}}')
        with mock.patch.object(ub, "_calibration_path", lambda: cal):
            first = ub._calibration_fingerprint()
            os.utime(cal, (0, 0))
            assert ub._calibration_fingerprint() == first
            with open(cal, "w", encoding="utf-8") as f:
                f.write('{"base_yaw": {"homing_offset": 1909}}')
            assert ub._calibration_fingerprint() != first


def test_the_calibration_path_comes_from_the_robot_not_a_second_derivation():
    """Two copies of the resolution rule can drift; the arm's own answer cannot."""
    import hal.app_state as state

    robot = mock.Mock()
    robot.calibration_fpath = "/var/lib/hal/calibration/robots/hal_follower/lamp-ac82.json"
    svc = mock.Mock()
    svc.robot = robot
    with mock.patch.object(state, "animation_service", svc, create=True):
        assert ub._calibration_path().endswith("lamp-ac82.json")


def test_it_falls_back_to_deriving_the_path_with_no_robot_connected():
    """Off-device tests, and the window before the arm connects."""
    import hal.app_state as state

    with mock.patch.object(state, "animation_service", None, create=True):
        path = ub._calibration_path()
    assert path is None or path.endswith(".json")
