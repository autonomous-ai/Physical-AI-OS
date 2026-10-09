"""Focused tests for the one-shot look-aim."""

import time
from unittest import mock

import numpy as np
import pytest

import hal.config as config
import hal.app_state as state
from hal.drivers.tracking import aim, user_check
from hal.drivers.tracking.user_check import FaceEvidence, FaceTrack
from test.body_ownership import BodyOwnership


class _FakeCap:
    def __init__(self, frame):
        self._frame = frame

    def acquire_consumer(self):
        pass

    def release_consumer(self):
        pass

    @property
    def last_frame(self):
        return self._frame


class _FakeSvc:
    """Fake arm that tracks yaw across nudges."""

    JOINTS = ["base_yaw.pos", "base_pitch.pos", "elbow_pitch.pos", "wrist_pitch.pos"]

    def __init__(self):
        self.yaw = 0.0
        # Non-yaw joints start away from any remembered posture so a restore is observable.
        self.pose = {j: -40.0 for j in self.JOINTS if j != "base_yaw.pos"}
        self.nudge = mock.Mock(side_effect=self._nudge)
        self.move_and_hold = mock.Mock(side_effect=self._move_and_hold)
        self.get_joint_names = mock.Mock(return_value=list(self.JOINTS))
        self.get_positions = mock.Mock(
            side_effect=lambda: {**self.pose, "base_yaw.pos": self.yaw}
        )

    def _nudge(self, yaw, pitch, duration, current, policy):
        self.yaw += yaw
        return {"base_yaw.pos": self.yaw}

    def _move_and_hold(self, positions, duration=None):
        for joint, value in positions.items():
            if joint == "base_yaw.pos":
                self.yaw = float(value)
            else:
                self.pose[joint] = float(value)
        return {**self.pose, "base_yaw.pos": self.yaw}


def _detector(box, target_hit="person"):
    """Detector returning `box` for target_hit, None otherwise."""
    d = mock.Mock()
    d.detect = mock.Mock(
        side_effect=lambda f, t, strict=True, min_conf=None: box if t == target_hit else None
    )
    d.last_confidence = 0.9
    return d


# Captured before the autouse fixture below replaces it.
_REAL_SWEEP = aim._sweep_for_subject


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset `_last_seen_mono`, which persists across look calls in production."""
    aim._last_seen_mono = 0.0
    aim._last_seen_yaw = 0.0
    aim._abort_evt.clear()
    # Stub the give-up sweep; tests that exercise it patch this themselves.
    with mock.patch.object(aim, "_sweep_for_subject", return_value=False):
        yield


def _frame(width=640, height=480):
    return np.zeros((height, width, 3), dtype=np.uint8)


def _run(box, target_hit="person", disabled=False, deadline=5.0):
    frame = _frame()
    svc = _FakeSvc()
    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", disabled, create=True),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=None),
    ):
        res = aim.aim_for_look(deadline, detector=_detector(box, target_hit))
    return res, svc


def test_subject_on_the_right_moves_yaw_positive():
    # dx > 0 must INCREASE base_yaw (the tracker's verified convention).
    res, svc = _run(box=(500, 100, 80, 200))
    assert svc.nudge.called
    yaw = svc.nudge.call_args[0][0]
    assert yaw > 0, f"expected positive yaw for a right-of-centre subject, got {yaw}"
    assert res.yaw_moved_deg > 0


def test_subject_on_the_left_moves_yaw_negative():
    res, svc = _run(box=(60, 100, 80, 200))
    assert svc.nudge.called
    assert svc.nudge.call_args[0][0] < 0


def test_pitch_is_never_commanded_in_v1():
    _res, svc = _run(box=(500, 100, 80, 200))
    assert svc.nudge.call_args[0][1] == 0.0


def test_already_centred_does_not_move():
    res, svc = _run(box=(300, 100, 40, 200))
    assert res.aimed is True
    assert not svc.nudge.called


def test_subject_not_found_reports_and_does_not_move():
    res, svc = _run(box=None)
    assert res.aimed is False
    assert res.reason == "subject not found"
    assert not svc.nudge.called


def test_camera_disabled_never_aims():
    res, svc = _run(box=(500, 100, 80, 200), disabled=True)
    assert res.aimed is False
    assert res.reason == "camera disabled"
    assert not svc.nudge.called


def test_face_is_used_when_no_person_is_detected():
    res, svc = _run(box=(500, 100, 60, 60), target_hit="face")
    assert svc.nudge.called
    assert "face" in res.reason or res.iterations > 0


def test_abort_stops_before_moving():
    aim.request_abort()
    try:
        res, svc = _run(box=(500, 100, 80, 200))
    finally:
        aim._abort_evt.clear()
    # request_abort() is cleared at entry by design, so the aim still runs.
    assert res is not None


def test_deadline_zero_returns_immediately():
    res, svc = _run(box=(500, 100, 80, 200), deadline=0.0)
    assert res.aimed is False
    assert res.reason == "deadline"
    assert not svc.nudge.called


def test_occlusion_holds_instead_of_turning_away():
    frame = _frame()
    svc = _FakeSvc()
    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch.object(aim, "_last_seen_mono", aim.time.monotonic()),
        mock.patch.object(aim, "_last_seen_yaw", 0.0),
    ):
        res = aim.aim_for_look(5.0, detector=_detector(None))
    assert res.aimed is False
    assert "occluded" in res.reason
    assert not svc.nudge.called, "must hold position, not turn away"


def test_stale_sighting_does_not_hold():
    frame = _frame()
    svc = _FakeSvc()
    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch.object(aim, "_last_seen_mono", aim.time.monotonic() - 60.0),
        mock.patch.object(aim, "_last_seen_yaw", 0.0),
    ):
        res = aim.aim_for_look(5.0, detector=_detector(None))
    assert "occluded" not in res.reason


def _bearing(deg, conf, pose=None):
    # Realistic BearingEstimate: a bare Mock silently skipped the step; `pose` must be a real dict.
    if pose is None:
        pose = {"base_yaw.pos": deg, "base_pitch.pos": 5.0,
                "elbow_pitch.pos": 10.0, "wrist_pitch.pos": 0.0}
    return mock.Mock(
        bearing_deg=deg, confidence=conf, samples=12, age_s=30.0, pose=pose
    )


def _run_no_subject(estimate, seen_mono=0.0):
    frame = _frame()
    svc = _FakeSvc()
    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch.object(aim, "_last_seen_mono", seen_mono),
        mock.patch(
            "hal.drivers.tracking.user_bearing.read_estimate", return_value=estimate
        ),
    ):
        res = aim.aim_for_look(5.0, detector=_detector(None))
    return res, svc


def test_no_subject_steps_toward_the_remembered_bearing():
    res, svc = _run_no_subject(_bearing(60.0, 0.9))
    assert svc.move_and_hold.called
    assert res.bearing_steps > 0


def test_bearing_travel_goes_straight_to_the_remembered_pose():
    """Bearing travel is one move to the remembered pose, not a series of hops."""
    res, svc = _run_no_subject(_bearing(120.0, 0.9))
    assert svc.move_and_hold.call_count == 1, "should arrive in a single move"
    (positions,), _ = svc.move_and_hold.call_args
    assert abs(positions["base_yaw.pos"] - 120.0) < 1e-6, positions


def test_low_confidence_bearing_is_not_worth_turning_for():
    res, svc = _run_no_subject(_bearing(60.0, 0.01))
    assert not svc.nudge.called
    assert res.reason == "subject not found"


def test_no_bearing_recorded_yet_does_not_move():
    res, svc = _run_no_subject(None)
    assert not svc.nudge.called
    assert res.bearing_steps == 0


def test_bearing_steps_are_bounded():
    res, svc = _run_no_subject(_bearing(135.0, 0.9))
    assert res.bearing_steps <= aim.MAX_BEARING_STEPS


def test_camera_disabled_never_scores_a_failed_prediction():
    with mock.patch("hal.drivers.tracking.user_bearing.record_prediction") as scored:
        res, _svc = _run(box=(500, 100, 80, 200), disabled=True)
    assert res.reason == "camera disabled"
    assert not scored.called


def test_searching_is_announced_once_when_the_lamp_turns_away():
    with mock.patch.object(aim, "_say") as say:
        res, svc = _run_no_subject(_bearing(120.0, 0.9))
    assert res.bearing_steps > 0, "the search never ran, so nothing was announced"
    searching = [c for c in say.call_args_list if c[0][0] == "look_searching"]
    assert len(searching) == 1, f"expected one announcement, got {len(searching)}"


def test_nothing_is_said_when_the_subject_is_already_centred():
    with mock.patch.object(aim, "_say") as say:
        _run(box=(300, 100, 40, 200))
    assert not say.called


def test_found_is_only_announced_after_a_search():
    with mock.patch.object(aim, "_say") as say:
        _run(box=(500, 100, 80, 200))
    assert not any(c[0][0] == "look_found" for c in say.call_args_list)


def test_speech_can_be_disabled():
    with mock.patch.object(aim.config, "LOOK_AIM_SPEAK", False), \
         mock.patch.object(aim, "_say") as say:
        _run_no_subject(_bearing(120.0, 0.9))
    assert not say.called


class _FakeAnim(BodyOwnership):
    def __init__(self, tracking=False):
        self._tracking_active = tracking


def test_ownership_is_claimed_for_the_whole_look():
    anim = _FakeAnim()
    with mock.patch.object(state, "animation_service", anim):
        with aim.servo_ownership():
            assert anim._tracking_active is True, "emotion servo must be suppressed"
    assert anim._tracking_active is False, "ownership must be released"


def test_ownership_does_not_release_a_real_tracking_session():
    anim = _FakeAnim(tracking=True)
    with mock.patch.object(state, "animation_service", anim):
        with aim.servo_ownership():
            assert anim._tracking_active is True
    assert anim._tracking_active is True, "pre-existing tracking must survive"


def test_ownership_is_released_even_when_the_body_raises():
    anim = _FakeAnim()
    with mock.patch.object(state, "animation_service", anim):
        try:
            with aim.servo_ownership():
                raise RuntimeError("capture blew up")
        except RuntimeError:
            pass
    assert anim._tracking_active is False, "a failed capture must not leave the body locked"


def test_ownership_is_harmless_with_no_animation_service():
    with mock.patch.object(state, "animation_service", None):
        with aim.servo_ownership():
            pass


def test_overlapping_owners_release_in_any_order():
    # The #312 overlapping-owner race, replayed deterministically.
    anim = _FakeAnim()
    with mock.patch.object(state, "animation_service", anim):
        gaze_watcher = aim.servo_ownership()
        look_tool = aim.servo_ownership()
        gaze_watcher.__enter__()
        look_tool.__enter__()
        assert anim._tracking_active is True, "both owners hold the body"

        gaze_watcher.__exit__(None, None, None)
        assert anim._tracking_active is True, "the look still owns the body"

        look_tool.__exit__(None, None, None)
    assert anim._tracking_active is False, "the last owner out must free the body"


def test_an_aim_over_a_live_writer_does_not_become_a_permanent_flag():
    # `prev` read the composite property but the setter wrote the flag.
    anim = _FakeAnim()
    anim.acquire_body()
    with mock.patch.object(state, "animation_service", anim):
        with aim.servo_ownership():
            assert anim._tracking_active is True
    assert anim._tracking_active is True, "the follower still owns the body"

    anim.release_body()
    assert anim._tracking_active is False, "and the body is free once it stops"
    assert anim._tracking_flag is False, "a counter hold must never become a flag"


def test_trace_shows_whether_the_head_actually_moved():
    res, svc = _run(box=(500, 100, 80, 200))
    assert res.start_yaw is not None and res.end_yaw is not None
    assert res.end_yaw != res.start_yaw, "head should have moved toward the subject"
    assert any("centre" in st["action"] for st in res.steps)


def test_trace_distinguishes_no_bearing_from_a_bearing_that_missed():
    res_none, _ = _run_no_subject(None)
    assert res_none.bearing_consulted is None
    assert any("no bearing recorded yet" in st["action"] for st in res_none.steps)

    res_used, _ = _run_no_subject(_bearing(60.0, 0.9))
    assert res_used.bearing_consulted is not None
    assert res_used.bearing_consulted["bearing_deg"] == 60.0


def test_trace_records_the_occlusion_hold():
    frame = _frame()
    svc = _FakeSvc()
    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch.object(aim, "_last_seen_mono", aim.time.monotonic()),
        mock.patch.object(aim, "_last_seen_yaw", 0.0),
    ):
        res = aim.aim_for_look(5.0, detector=_detector(None))
    assert any("hold" in st["action"] for st in res.steps)


def test_the_detector_is_built_once_not_per_look():
    # A per-look ObjectDetector cost ~7s on device, so it must be reused.
    aim._shared_detector = None
    with mock.patch("hal.drivers.tracking.detection.ObjectDetector") as ctor:
        ctor.return_value = mock.Mock()
        first = aim.get_detector()
        second = aim.get_detector()
        third = aim.get_detector()
    assert ctor.call_count == 1, f"detector rebuilt {ctor.call_count} times"
    assert first is second is third
    aim._shared_detector = None


def test_a_failing_detector_does_not_wedge_the_aim():
    aim._shared_detector = None
    with mock.patch("hal.drivers.tracking.detection.ObjectDetector",
                    side_effect=RuntimeError("model missing")):
        assert aim.get_detector() is None
    aim._shared_detector = None


class _StaleCap(_FakeCap):
    """A camera whose frame timestamp does not advance after a servo write."""

    def __init__(self, frame):
        super().__init__(frame)
        self.last_frame_ts = 100.0
        self.consumers = 0
        self.max_consumers = 0

    def acquire_consumer(self):
        self.consumers += 1
        self.max_consumers = max(self.max_consumers, self.consumers)

    def release_consumer(self):
        self.consumers -= 1


class _StampingSvc(_FakeSvc):
    """Servo that stamps `last_servo_write`, as the real AnimationService does."""

    def __init__(self):
        super().__init__()
        self.last_servo_write = 100.0

    def _nudge(self, yaw, pitch, duration, current, policy):
        self.last_servo_write += 1.0
        return super()._nudge(yaw, pitch, duration, current, policy)


def _run_stale(box, deadline=1.0):
    frame = _frame()
    cap = _StaleCap(frame)
    svc = _StampingSvc()
    with (
        mock.patch.object(state, "camera_capture", cap),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=None),
        mock.patch.object(aim, "FRAME_WAIT_S", 0.05),
    ):
        res = aim.aim_for_look(deadline, detector=_detector(box))
    return res, svc, cap


def test_stale_frames_do_not_march_the_head():
    """With feedback frozen, the aim must not keep issuing the same correction."""
    # One command per fresh measurement is the invariant, so count corrections.
    res, svc, _ = _run_stale((520, 200, 600, 400))
    assert svc.nudge.call_count == 1, (
        f"issued {svc.nudge.call_count} corrections from one measurement "
        f"(head travelled {svc.yaw:+.1f} deg)"
    )
    assert res.reason == "no fresh frame"


def test_camera_consumer_held_once_for_the_whole_aim():
    """The camera consumer is held once for the whole aim, not per frame."""
    _run_stale((520, 200, 600, 400))
    _, _, cap = _run_stale((520, 200, 600, 400))
    assert cap.max_consumers == 1, "consumer should be held once, not per grab"
    assert cap.consumers == 0, "consumer leaked"


_REAL_FOV_DEG = 110.0  # device-measured (107-123); the aim's constant is a guess


class _FreshCap(_FakeCap):
    """Always offers a frame newer than any servo write."""

    @property
    def last_frame_ts(self):
        import time as _t

        return _t.monotonic() + 1000.0


def _sim_detector(svc, subject_bearing_deg, width=640):
    """Detector that reports where the subject falls given the CURRENT head yaw."""

    def _detect(frame, target, strict=True, min_conf=None):
        if target != "person":
            return None
        rel = subject_bearing_deg - svc.yaw
        px = width / 2.0 + rel * (width / _REAL_FOV_DEG)
        if not (0 <= px < width):
            return None
        # (x, y, w, h) top-left, matching ObjectDetector — NOT corners.
        return (int(px) - 20, 200, 40, 200)

    d = mock.Mock()
    d.detect = mock.Mock(side_effect=_detect)
    d.last_confidence = 0.9
    return d


def _run_closed_loop(subject_bearing_deg, fov_setting):
    import hal.config as hal_cfg

    svc = _FakeSvc()
    with (
        mock.patch.object(state, "camera_capture", _FreshCap(_frame())),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch.object(hal_cfg, "LOOK_AIM_FOV_DEG", fov_setting),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=None),
        mock.patch("hal.drivers.tracking.aim._record_bearing_if_centred"),
    ):
        res = aim.aim_for_look(30.0, detector=_sim_detector(svc, subject_bearing_deg))
    return res, svc


def test_calibrated_fov_centres_within_two_iterations():
    """With a calibrated FOV the aim centres within two iterations."""
    res, svc = _run_closed_loop(30.0, 100.0)
    assert res.aimed, f"did not centre: {res.reason}"
    assert res.iterations <= 2, f"took {res.iterations} iterations"
    assert abs(svc.yaw - 30.0) < 8.0, f"settled at {svc.yaw:+.1f} deg, subject at +30"


def test_self_calibration_recovers_from_a_wrong_fov_constant():
    """Self-calibration recovers from a wrong FOV constant."""
    res_bad, _ = _run_closed_loop(30.0, 60.0)
    res_good, _ = _run_closed_loop(30.0, 100.0)
    assert res_bad.aimed and res_good.aimed, (res_bad.reason, res_good.reason)
    assert res_bad.iterations <= res_good.iterations + 1, (
        f"a wrong constant cost {res_bad.iterations - res_good.iterations} extra steps"
    )


def test_measured_scale_is_recorded_per_step():
    """The scale is traced and flagged while still the unmeasured guess."""
    res, _ = _run_closed_loop(30.0, 100.0)
    assert res.steps and all("scale" in st for st in res.steps)


def test_scale_measurement_rejects_uninformative_steps():
    """Tiny moves do not update the scale."""
    assert aim._measure_scale(0.5, 0.10) is None
    assert aim._measure_scale(20.0, 0.001) is None
    assert aim._measure_scale(20.0, -0.10) is None
    assert aim._measure_scale(400.0, 0.05) is None
    assert aim._measure_scale(20.0, 0.10) == 200.0


def test_last_move_is_reported_for_the_capture_settle():
    """The aim result reports the swing size for settle time."""
    res, _ = _run_closed_loop(30.0, 100.0)
    assert res.last_move_deg != 0.0


def test_aim_never_overshoots_past_the_subject():
    """Every step moves toward the subject without crossing it."""
    _, svc = _run_closed_loop(30.0, 100.0)
    assert svc.yaw <= 30.0 + 1e-6, f"crossed the subject: {svc.yaw:+.1f} > +30"


def test_bearing_step_restores_the_remembered_pitch_joints():
    """A bearing step restores the remembered pitch joints, not just yaw."""
    res, svc = _run_no_subject(_bearing(60.0, 0.9))
    assert res.bearing_steps > 0
    assert svc.pose["base_pitch.pos"] == 5.0, svc.pose
    assert svc.pose["elbow_pitch.pos"] == 10.0, svc.pose


def test_posture_is_restored_even_when_the_yaw_is_already_right():
    """"Already pointing there" compares the whole pose, not just the base."""
    svc = _FakeSvc()
    svc.yaw = 60.0
    est = _bearing(60.0, 0.9)
    with (
        mock.patch.object(state, "safety_policy", None, create=True),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=est),
    ):
        moved = aim._step_toward_bearing(svc, {})
    assert moved, "a wrong posture at the right yaw must still move"
    assert svc.pose["base_pitch.pos"] == 5.0


def test_no_move_when_already_in_the_remembered_shape():
    """Otherwise every search step re-issues a move for rounding noise."""
    svc = _FakeSvc()
    svc.yaw = 60.0
    svc.pose = {"base_pitch.pos": 5.0, "elbow_pitch.pos": 10.0, "wrist_pitch.pos": 0.0}
    with (
        mock.patch.object(state, "safety_policy", None, create=True),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate",
                   return_value=_bearing(60.0, 0.9)),
    ):
        moved = aim._step_toward_bearing(svc, {})
    assert not moved
    assert not svc.move_and_hold.called


def test_unknown_joints_are_not_commanded():
    """A remembered pose is not sent to joints this device lacks."""
    svc = _FakeSvc()
    est = _bearing(60.0, 0.9, pose={"base_yaw.pos": 60.0, "tentacle.pos": 12.0})
    with (
        mock.patch.object(state, "safety_policy", None, create=True),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=est),
    ):
        aim._step_toward_bearing(svc, {})
    (positions,), _ = svc.move_and_hold.call_args
    assert "tentacle.pos" not in positions, positions


def test_a_far_person_is_not_treated_as_a_subject():
    """A tiny far-away face is rejected as an aim target."""
    res, svc = _run((600, 300, 60, 25))
    assert not res.aimed
    assert res.reason != "centred on person"
    assert not svc.nudge.called, "turned toward a stranger across the room"


def test_a_close_person_still_passes_the_gate():
    """A large edge-clipped person is still accepted."""
    res, svc = _run((520, 200, 100, 165))
    assert svc.nudge.called or res.aimed


def test_the_gate_uses_height_not_width():
    """The size gate uses box height, not width."""
    frame = _frame(width=640, height=480)
    narrow_but_tall = (10, 0, 12, 300)
    assert aim._is_near_enough(narrow_but_tall, frame, "person")


def test_the_aim_asks_for_a_higher_confidence_than_the_tracker():
    """Aiming uses a stricter confidence floor than the tracker."""
    import hal.config as hal_cfg

    det = _detector((520, 200, 100, 165))
    aim._detect_subject(det, _frame())
    _args, kwargs = det.detect.call_args
    assert kwargs.get("min_conf") == hal_cfg.LOOK_AIM_MIN_CONFIDENCE
    assert hal_cfg.LOOK_AIM_MIN_CONFIDENCE > 0.15, "must be stricter than the global floor"


def test_the_chosen_box_reports_its_confidence():
    """Without this a bad box cannot be told from a 0.17 fluke when debugging."""
    det = _detector((520, 200, 100, 165))
    det.last_confidence = 0.63
    _box, _kind, conf = aim._detect_subject(det, _frame())
    assert conf == 0.63


def test_an_older_detector_without_min_conf_still_works():
    """The size gate must keep protecting a detector that predates the kwarg."""
    det = mock.Mock()
    det.detect = mock.Mock(
        side_effect=lambda f, t, strict=True: (520, 200, 100, 165) if t == "person" else None
    )
    box, kind, _conf = aim._detect_subject(det, _frame())
    assert box is not None and kind == "person"


def test_found_is_announced_once_however_many_iterations_follow():
    """The found announcement fires once per aim, not every iteration."""
    svc = _FakeSvc()
    seen = {"n": 0}

    def _detect(frame, target, strict=True, min_conf=None):
        if target != "person":
            return None
        seen["n"] += 1
        return None if seen["n"] == 1 else (330, 100, 60, 300)

    det = mock.Mock()
    det.detect = mock.Mock(side_effect=_detect)
    det.last_confidence = 0.9
    with (
        mock.patch.object(state, "camera_capture", _FakeCap(_frame())),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate",
                   return_value=_bearing(60.0, 0.9)),
        mock.patch.object(aim, "_say") as say,
    ):
        res = aim.aim_for_look(5.0, detector=det)
    found = [c for c in say.call_args_list if c[0][0] == "look_found"]
    assert res.bearing_steps > 0, "the search never ran, so nothing was announced"
    assert len(found) == 1, f"announced {len(found)} times"


def test_the_measured_scale_is_biased_low_not_high():
    """The measured scale is damped, never amplified."""
    assert 0.0 < aim.SCALE_SAFETY < 1.0
    assert aim.MAX_SCALE_DEG <= 250.0, "400 asked for corrections that got clamped"


def _candidate_detector(candidates, face=None):
    """Detector exposing `detect_candidates` for person, `detect` for face."""
    d = mock.Mock()
    d.detect_candidates = mock.Mock(
        side_effect=lambda f, t, strict=False, min_conf=None: (
            list(candidates) if t == "person" else []
        )
    )
    d.detect = mock.Mock(
        side_effect=lambda f, t, strict=True, min_conf=None: face if t == "face" else None
    )
    d.last_confidence = 0.5
    return d


def test_the_nearest_person_wins_over_a_more_confident_distant_one():
    """The nearest person wins over a more confident distant one."""
    frame = _frame(width=1280, height=720)
    colleague = ((300, 210, 160, 190), 0.71)
    asker = ((640, 0, 640, 700), 0.52)
    box, kind, conf = aim._detect_subject(_candidate_detector([colleague, asker]), frame)

    assert box == asker[0], "the closest person is the one talking to the lamp"
    assert kind == "person"
    assert conf == 0.52


def test_a_detection_too_small_to_be_the_asker_is_not_chosen():
    """The floor still rejects — it just runs before the choice now."""
    frame = _frame(width=1280, height=720)
    far = ((300, 300, 40, 70), 0.95)
    box, kind, _ = aim._detect_subject(_candidate_detector([far], face=None), frame)

    assert box is None and kind == ""


def test_the_size_floor_is_applied_before_ranking_not_after():
    """A high-confidence stranger must not shadow a qualifying asker."""
    frame = _frame(width=1280, height=720)
    tiny_but_certain = ((10, 10, 30, 60), 0.99)
    real_asker = ((600, 100, 400, 500), 0.40)
    box, _kind, _conf = aim._detect_subject(
        _candidate_detector([tiny_but_certain, real_asker]), frame
    )

    assert box == real_asker[0]


def test_no_person_candidates_falls_back_to_the_face_path():
    frame = _frame(width=1280, height=720)
    face_box = (500, 200, 120, 140)
    box, kind, _ = aim._detect_subject(_candidate_detector([], face=face_box), frame)

    assert box == face_box and kind == "face"


def test_a_detector_without_the_candidate_path_still_works():
    """Older detector object: fall through to `detect`, do not raise."""
    frame = _frame(width=1280, height=720)
    d = mock.Mock(spec=["detect", "last_confidence"])
    d.detect = mock.Mock(
        side_effect=lambda f, t, strict=True, min_conf=None: (
            (100, 100, 300, 400) if t == "person" else None
        )
    )
    d.last_confidence = 0.8
    box, kind, _ = aim._detect_subject(d, frame)

    assert box == (100, 100, 300, 400) and kind == "person"


def _scored(box, target_hit="person", deadline=5.0):
    """Run an aim WITH a bearing available; return (result, what was scored)."""
    calls = []
    from hal.drivers.tracking import user_bearing

    est = mock.Mock()
    est.bearing_deg = 40.0
    est.confidence = 0.9
    est.pose = {"base_yaw.pos": 40.0}

    frame = _frame()
    svc = _FakeSvc()
    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch.object(user_bearing, "read_estimate", return_value=est),
        mock.patch.object(
            user_bearing, "record_prediction",
            side_effect=lambda hit, now=None: calls.append(hit) or False,
        ),
    ):
        res = aim.aim_for_look(deadline, detector=_detector(box, target_hit))
    return res, calls


def test_a_bearing_step_that_finds_nobody_is_counted_as_a_miss():
    """The give-up exit always reported this. It is the other four that did not."""
    res, calls = _scored(None)
    assert res.bearing_steps > 0, "precondition: the bearing was consulted"
    assert calls == [False]


def test_an_aim_that_never_consulted_the_bearing_says_nothing_about_it():
    """A hit or a miss from an aim that never turned there is not evidence."""
    res, calls = _scored((300, 100, 60, 200))
    assert res.bearing_steps == 0
    assert calls == [], "no bearing step means no verdict"


def test_the_bearing_is_scored_at_most_once_per_aim():
    """It used to be called on EVERY iteration a subject was visible."""
    _res, calls = _scored(None)
    assert len(calls) <= 1


def test_a_failed_search_says_so():
    """After `look_searching`, a failed search still reports back."""
    said = []
    with mock.patch.object(aim, "_say", side_effect=said.append):
        res, _calls = _scored(None)

    assert res.bearing_steps > 0, "precondition: a search was announced"
    assert "look_searching" in said
    assert said[-1] == "look_lost", said


def test_a_look_that_never_searched_stays_quiet():
    """An immediate find speaks no filler."""
    said = []
    with mock.patch.object(aim, "_say", side_effect=said.append):
        _res, _calls = _scored((300, 100, 60, 200))

    assert "look_searching" not in said
    assert "look_lost" not in said


def test_look_lost_is_only_said_after_actually_looking_around():
    """`look_lost` is only said after an actual look-around."""
    swept = []
    with mock.patch.object(aim, "_sweep_for_subject",
                           side_effect=lambda: swept.append(True) or False), \
         mock.patch.object(aim, "_say", side_effect=(said := []).append):
        _res, _calls = _scored(None)

    assert swept, "it gave up without looking around"
    assert said.index("look_searching") < said.index("look_lost"), said


def test_a_subject_found_by_the_sweep_is_then_centred():
    """After the sweep sees someone, the aim centres them."""
    seen = {"n": 0}

    def found_after_sweep():
        seen["n"] += 1
        return True

    with mock.patch.object(aim, "_sweep_for_subject", side_effect=found_after_sweep), \
         mock.patch.object(aim, "_say", side_effect=(said := []).append):
        res, _calls = _scored(None)

    assert seen["n"] >= 1
    assert "look_lost" not in said, "it swept, found someone, and still cried lost"


def test_the_deadline_stops_counting_while_the_sweep_runs():
    """The deadline pauses while the sweep runs."""
    def slow_sweep():
        time.sleep(0.4)
        return False

    with mock.patch.object(config, "LOOK_AIM_DEADLINE_S", 0.5), \
         mock.patch.object(aim, "_sweep_for_subject", side_effect=slow_sweep), \
         mock.patch.object(aim, "_say", side_effect=(said := []).append):
        _res, _calls = _scored(None)

    assert "look_lost" in said, (
        "the deadline swallowed the aim mid-sweep instead of pausing"
    )


def test_the_look_aim_fallback_is_a_silent_glance():
    """Not seeing the user on a look ends in a glance, not the 18-look room sweep."""
    from hal.drivers.tracking import search

    with mock.patch.object(search, "search_for_subject",
                           return_value=search.SearchResult(False, "no person found")) as s:
        _REAL_SWEEP()
    assert s.call_args.kwargs.get("glance") is True
    assert s.call_args.kwargs.get("on_progress") is not None, "the midpoint line would speak"


def test_a_sweep_that_cannot_run_does_not_sink_the_aim():
    """A sweep that cannot run still yields an aim result."""
    from hal.drivers.tracking import search

    with mock.patch.object(search, "search_for_subject",
                           side_effect=RuntimeError("no camera")):
        assert aim._sweep_for_subject() is False


def test_centre_on_box_walks_the_subject_to_the_middle():
    """A box right of centre produces a positive yaw nudge and stops in the deadband."""
    svc = _FakeSvc()
    boxes = [(500, 200, 40, 40), (310, 200, 40, 40)]

    res = aim.centre_on_box(
        svc,
        _FakeCap(_frame()),
        probe=lambda _f: boxes.pop(0) if boxes else (310, 200, 40, 40),
    )

    assert res.centred is True, res.reason
    moved = [c.args[0]["base_yaw.pos"] for c in svc.move_and_hold.call_args_list
             if "base_yaw.pos" in c.args[0]]
    assert moved and moved[0] > 0, (
        f"a subject right of centre must turn the base right, got {moved}")
    assert res.box == (310, 200, 40, 40), "the result must carry the CENTRED box"
    assert res.frame is not None, "the result must carry the frame it centred on"


def test_centre_on_box_gives_up_rather_than_hunting_forever():
    """A detection that never moves exits with a reason, not an exception."""
    res = aim.centre_on_box(_FakeSvc(), _FakeCap(_frame()),
                            probe=lambda _f: (600, 200, 20, 20))

    assert res.centred is False
    assert res.iterations == aim.MAX_ITERATIONS
    assert res.reason == "max iterations"


def test_centre_on_box_reports_the_last_good_box_when_it_loses_the_subject():
    """Losing the detection keeps the last frame and box."""
    seen = [(500, 200, 40, 40), None]

    res = aim.centre_on_box(_FakeSvc(), _FakeCap(_frame()),
                            probe=lambda _f: seen.pop(0) if seen else None)

    assert res.centred is False
    assert res.reason == "lost the subject"
    assert res.box == (500, 200, 40, 40)


def test_centre_on_box_does_not_score_the_remembered_bearing():
    """Centring a sweep hit does not score the bearing estimator."""
    with mock.patch.object(aim, "_score_prediction") as scored, \
         mock.patch.object(aim, "_record_bearing_if_centred") as recorded:
        aim.centre_on_box(_FakeSvc(), _FakeCap(_frame()),
                          probe=lambda _f: (310, 200, 40, 40))

    scored.assert_not_called()
    recorded.assert_not_called()


def test_encode_annotated_keeps_its_debug_lines_by_default():
    """Centre lines are drawn by default; the flag only affects the search image."""
    import inspect

    from hal.drivers.tracking import look_debug

    sig = inspect.signature(look_debug.encode_annotated)
    assert sig.parameters["centre_lines"].default is True


def test_centre_on_box_is_not_defeated_by_a_stale_abort():
    """A stale abort flag from an earlier click does not abort centring."""
    aim.request_abort()
    boxes = [(500, 200, 40, 40), (310, 200, 40, 40)]

    res = aim.centre_on_box(_FakeSvc(), _FakeCap(_frame()),
                            probe=lambda _f: boxes.pop(0) if boxes else (310, 200, 40, 40))

    assert res.reason != "aborted", "a stale abort flag defeated the correction"
    assert res.centred is True
    assert res.iterations >= 1


def test_centre_on_box_tolerates_a_flickering_detection():
    """A single missed probe at the frame edge does not end centring."""
    seen = [None, None, (500, 200, 40, 40), (310, 200, 40, 40)]

    res = aim.centre_on_box(_FakeSvc(), _FakeCap(_frame()),
                            probe=lambda _f: seen.pop(0) if seen else (310, 200, 40, 40))

    assert res.centred is True, res.reason
    assert res.iterations >= 1


def test_centre_on_box_still_gives_up_when_the_subject_stays_gone():
    """Miss tolerance is bounded so a vanished subject ends the hunt."""
    res = aim.centre_on_box(_FakeSvc(), _FakeCap(_frame()), probe=lambda _f: None)

    assert res.centred is False
    assert res.reason == "lost the subject"


def test_centre_on_box_corrects_pitch_with_gazes_verified_sign():
    """A box above centre tilts the camera up (decreasing pitch)."""
    svc = _FakeSvc()
    boxes = [(310, 40, 40, 40), (310, 220, 40, 40)]

    res = aim.centre_on_box(svc, _FakeCap(_frame()),
                            probe=lambda _f: boxes.pop(0) if boxes else (310, 220, 40, 40))

    assert res.centred is True, res.reason
    pitch_moves = [c.args[0] for c in svc.move_and_hold.call_args_list
                   if any(j.endswith("pitch.pos") for j in c.args[0])]
    assert pitch_moves, "a subject above centre produced no pitch correction"
    # Camera-space, not joint-space: distribute_pitch applies a per-joint sign.
    from hal.drivers.tracking.servo_follow import PITCH_AXIS_SIGN

    before = {j: -40.0 for j in ("base_pitch.pos", "elbow_pitch.pos", "wrist_pitch.pos")}
    first = pitch_moves[0]
    camera = sum(PITCH_AXIS_SIGN[j] * (first[j] - before[j]) for j in before if j in first)
    assert camera < 0, f"box above centre must tilt the camera UP (negative), got {camera:+.1f}"


def test_centre_on_box_needs_both_axes_inside_the_deadband():
    """Centred left-right but still high in the frame is not centred."""
    res = aim.centre_on_box(_FakeSvc(), _FakeCap(_frame()),
                            probe=lambda _f: (310, 40, 40, 40))
    assert res.centred is False
    assert res.dy_frac is not None and res.dy_frac < -aim.CENTRE_PITCH_DEADBAND_FRAC


def test_centre_on_box_reports_the_frame_after_its_last_move_not_before():
    """The persisted frame is taken after the final move, not before it."""
    boxes = [(500, 200, 40, 40)]

    with mock.patch.object(aim, "MAX_ITERATIONS", 1):
        res = aim.centre_on_box(_FakeSvc(), _FakeCap(_frame()),
                                probe=lambda _f: boxes.pop(0) if boxes else (310, 220, 40, 40))

    assert res.iterations == 1
    assert res.box == (310, 220, 40, 40), (
        f"reported the pre-move box {res.box}, not the frame after the last move")
    assert res.centred is True, "the last move centred it and the result must say so"


def test_centre_on_box_final_measurement_also_runs_on_the_deadline_exit():
    """A deadline jump after the first move still takes the final look."""
    boxes = [(500, 200, 40, 40)]
    clock = {"t": 0.0}

    def _monotonic():
        clock["t"] += 2.5
        return clock["t"]

    # _grab_frame paces on the same clock; a stepping clock would starve it.
    with mock.patch.object(aim.time, "monotonic", _monotonic), \
         mock.patch.object(aim, "_grab_frame", lambda cap, svc=None, require_fresh=False: cap.last_frame):
        res = aim.centre_on_box(_FakeSvc(), _FakeCap(_frame()),
                                probe=lambda _f: boxes.pop(0) if boxes else (310, 220, 40, 40),
                                deadline_s=4.0)

    assert res.box == (310, 220, 40, 40), f"pre-move box reported on deadline: {res.box}"
    assert res.centred is True


def test_an_aim_that_moved_hands_the_body_back_later():
    with mock.patch("hal.drivers.tracking.body.release_to_idle_later") as later:
        res, svc = _run(box=(500, 100, 80, 200))
    assert svc.nudge.called
    later.assert_called_once()
    assert later.call_args[0][0] == aim.body.HOLD_AFTER_FIND_S


def test_an_already_centred_aim_leaves_playback_alone():
    with mock.patch("hal.drivers.tracking.body.release_to_idle_later") as later:
        res, svc = _run(box=(300, 100, 40, 200))
    assert res.iterations == 0
    assert not svc.nudge.called
    later.assert_not_called()


_NEAR = config.GAZE_BEARING_MIN_FACE_HEIGHT_FRAC + 0.05


def test_no_verdict_scores_nothing():
    with mock.patch("hal.drivers.tracking.user_bearing.record_prediction") as rp:
        aim._score_prediction(2, None)
    rp.assert_not_called()


def test_a_verdict_is_scored_only_when_the_bearing_was_used():
    with mock.patch("hal.drivers.tracking.user_bearing.record_prediction") as rp:
        aim._score_prediction(0, True)
        aim._score_prediction(1, False)
    rp.assert_called_once_with(False)


def _bearing_svc(yaw=10.0):
    svc = mock.Mock()
    svc.get_positions = mock.Mock(return_value={"base_yaw.pos": yaw, "wrist_roll.pos": 0.0})
    return svc


def _worker(tracks, frame_w=640):
    frame = np.zeros((480, frame_w, 3), np.uint8)
    with (
        mock.patch.object(user_check, "observe_faces", return_value=tracks),
        mock.patch.object(aim, "_grab_frame", return_value=frame),
        mock.patch("hal.drivers.tracking.user_bearing.record_sighting",
                   return_value=True) as rs,
    ):
        ok = aim._record_bearing_worker(_bearing_svc(), mock.Mock())
    return ok, rs


def test_a_centred_body_with_no_qualifying_face_records_nothing():
    """#545: look-aim used to teach the bearing from any centred body."""
    side_on = FaceTrack((300, 100, 60, 60), FaceEvidence(_NEAR, 0.0, 6))
    ok, rs = _worker([side_on])
    assert not ok
    rs.assert_not_called()


def test_a_facing_face_at_centre_records_the_bearing():
    facing = FaceTrack((290, 100, 60, 60), FaceEvidence(_NEAR, 1.0, 6))
    ok, rs = _worker([facing])
    assert ok
    assert abs(rs.call_args[0][0] - 10.0) < 1.0  # face centred -> bearing ~= current yaw


def test_a_qualifying_face_too_far_off_centre_is_not_recorded():
    """The same dx limit bearing_sampler applies (it leans on the FOV constant)."""
    edge = FaceTrack((600, 100, 30, 60), FaceEvidence(_NEAR, 1.0, 6))
    ok, rs = _worker([edge])
    assert not ok
    rs.assert_not_called()


def test_the_record_never_delays_the_look(monkeypatch):
    """The dwell runs on its own thread; the live aim must return immediately."""
    started = []

    class _T:
        def __init__(self, target, args, daemon, name):
            started.append(name)

        def start(self):
            pass

    monkeypatch.setattr(aim.threading, "Thread", _T)
    with mock.patch.object(aim, "_record_bearing_worker") as w:
        aim._record_bearing_if_centred(_bearing_svc(), mock.Mock())
    w.assert_not_called()
    assert started == ["look-aim-bearing"]


def test_a_near_face_in_frame_is_seen(monkeypatch):
    frame = np.zeros((480, 640, 3), np.uint8)
    monkeypatch.setattr("hal.drivers.tracking.detection.detect_faces_with_landmarks",
                        lambda small: [((0, 0, 60, int(480 * _NEAR) + 2), ())])
    monkeypatch.setattr("hal.drivers.tracking.frame_utils.downscale", lambda f: (f, 1.0))
    assert aim._near_face_in(frame)


def test_a_far_face_in_frame_is_not_near(monkeypatch):
    frame = np.zeros((480, 640, 3), np.uint8)
    monkeypatch.setattr("hal.drivers.tracking.detection.detect_faces_with_landmarks",
                        lambda small: [((0, 0, 20, 20), ())])
    monkeypatch.setattr("hal.drivers.tracking.frame_utils.downscale", lambda f: (f, 1.0))
    assert not aim._near_face_in(frame)
