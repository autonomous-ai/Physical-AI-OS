"""Focused tests for the deliberate search sweep."""

import os
import time
from unittest import mock

import numpy as np
import pytest

import hal.app_state as state
import hal.config as config
from hal.drivers.tracking import constants as C
from hal.drivers.tracking import search, user_check
from hal.drivers.tracking.aim import CentreResult
from hal.drivers.tracking.user_check import FaceEvidence, FaceTrack
from test.body_ownership import BodyOwnership


@pytest.fixture(autouse=True)
def _reset():
    search._abort_evt.clear()
    yield


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


class _FakeSvc(BodyOwnership):
    IDLE_BASELINE = {
        "base_yaw.pos": 3.0, "base_pitch.pos": 29.8, "elbow_pitch.pos": 27.1,
        "wrist_pitch.pos": -61.7, "wrist_roll.pos": 8.2,
    }

    def __init__(self, idle_baseline=None):
        self.yaw = 0.0
        self.roll = 0.0
        # Tracks wrist_pitch: consecutive looks can differ only in pitch.
        self.wrist_pitch = 0.0
        self.holds = []
        # (yaw, roll) after every commanded move, in order.
        self.trail = []
        # (joint, speed) writes, in order.
        self.speeds = []
        self._idle_baseline = (
            dict(self.IDLE_BASELINE) if idle_baseline is None else idle_baseline
        )
        self.nudge = mock.Mock(side_effect=self._nudge)
        # Report every joint: a joint absent from the pose counts as already correct.
        self.get_positions = mock.Mock(side_effect=lambda: {
            "base_yaw.pos": self.yaw,
            "base_pitch.pos": 0.0,
            "elbow_pitch.pos": 0.0,
            "wrist_pitch.pos": self.wrist_pitch,
            "wrist_roll.pos": self.roll,
        })

    def get_joint_names(self):
        return ["base_yaw.pos", "base_pitch.pos", "elbow_pitch.pos",
                "wrist_pitch.pos", "wrist_roll.pos"]

    UNWRITTEN_SPEED_EQUIVALENT = 0

    # Needed for the real release_to_idle handback path.
    idle_recording = "idle"

    def dispatch(self, cmd, payload):
        pass

    def set_joint_speed(self, motor_name, speed):
        self.speeds.append((motor_name, speed))
        return True

    def move_and_hold(self, target, duration=None):
        self.holds.append(dict(target))
        if "base_yaw.pos" in target:
            self.yaw = float(target["base_yaw.pos"])
        if "wrist_roll.pos" in target:
            self.roll = float(target["wrist_roll.pos"])
        if "wrist_pitch.pos" in target:
            self.wrist_pitch = float(target["wrist_pitch.pos"])
        self.trail.append((self.yaw, self.roll))

    def _nudge(self, y, p, d, cur, pol):
        self.yaw += y
        self.trail.append((self.yaw, self.roll))
        return {"base_yaw.pos": self.yaw}


def _run(detect_at_stop=None, bearing=None, disabled=False, abort_at_stop=None,
         confidence=0.9, pose=None, idle_baseline=None, on_progress=None):
    """detect_at_stop: 1-based stop index at which a subject appears."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    svc = _FakeSvc(idle_baseline=idle_baseline)
    calls = {"n": 0}

    def _detect(f, t, strict=True):
        # _detect_subject probes person then face, so count only the first probe.
        if t == "person":
            calls["n"] += 1
            if abort_at_stop is not None and calls["n"] >= abort_at_stop:
                search.request_abort()
        if detect_at_stop is not None and calls["n"] >= detect_at_stop:
            return (300, 100, 40, 200) if t == "person" else None
        return None

    det = mock.Mock()
    det.detect = mock.Mock(side_effect=_detect)
    if bearing is None:
        est = None
    else:
        est = mock.Mock(bearing_deg=bearing, confidence=confidence)
        est.pose = {"base_yaw.pos": bearing} if pose is None else pose

    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", disabled, create=True),
        mock.patch.object(search.time, "sleep"),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=est),
    ):
        res = search.search_for_subject(detector=det, on_progress=on_progress)
    return res, svc


def test_stops_overlap_so_nobody_falls_between_them():
    """Adjacent sweep tiles overlap."""
    reach = max(abs(r) for r, _ in search.LOOK_CIRCLE) + config.LOOK_AIM_FOV_DEG / 2.0
    assert search.STEP_DEG < 2 * reach, (
        f"step {search.STEP_DEG} leaves a seam between stops reaching +/-{reach}"
    )


def test_the_sweep_checks_the_remembered_bearing_first():
    """The remembered bearing is checked first, so the first subject seen is the one asked about."""
    stops = search._stop_list(60.0)
    assert stops[0] == 60.0, "the likely place must be checked first"


def test_the_sweep_goes_right_before_left():
    """The sweep goes right before left."""
    assert search._stop_list(0.0) == [0.0, search.STEP_DEG, -search.STEP_DEG]


def test_stops_stay_inside_the_mechanical_range():
    for seed in (-135.0, 0.0, 135.0):
        for y in search._stop_list(seed):
            assert C.YAW_MIN <= y <= C.YAW_MAX, f"{y} outside servo limits"


def test_seed_beyond_the_limit_is_clamped():
    stops = search._stop_list(999.0)
    assert max(stops) == C.YAW_MAX
    assert all(C.YAW_MIN <= y <= C.YAW_MAX for y in stops)


def test_a_stop_past_the_limit_is_clamped_not_dropped():
    """Out-of-range stops are clamped, not discarded."""
    stops = search._stop_list(C.YAW_MAX - 10.0)
    assert len(stops) == 3, f"a stop was dropped instead of clamped: {stops}"
    assert C.YAW_MAX in stops


def test_stops_on_first_sighting_rather_than_completing_the_sweep():
    res, svc = _run(detect_at_stop=2)
    assert res.found is True
    assert res.looks_visited == 2, "should stop as soon as it sees someone"


def test_reports_failure_after_exhausting_the_sweep():
    res, svc = _run(detect_at_stop=None)
    assert res.found is False
    assert res.reason == "no person found"
    assert res.looks_visited > 1


def test_camera_disabled_never_sweeps():
    res, svc = _run(detect_at_stop=1, disabled=True)
    assert res.found is False
    assert res.reason == "camera disabled"
    assert not svc.nudge.called


def test_abort_stops_the_sweep_mid_flight():
    res, svc = _run(detect_at_stop=None, abort_at_stop=2)
    assert res.reason == "aborted"
    assert res.looks_visited < search.MAX_STOPS


def test_a_stale_abort_does_not_block_the_next_search():
    search.request_abort()
    res, _svc = _run(detect_at_stop=1)
    assert res.found is True


def test_the_sweep_restores_the_remembered_posture_first():
    """The sweep restores the remembered posture before sweeping."""
    _res, svc = _run(
        bearing=40.0,
        pose={"base_yaw.pos": 40.0, "base_pitch.pos": -12.0, "wrist_pitch.pos": -30.0},
    )
    assert svc.holds, "no posture was restored before sweeping"
    restored = svc.holds[0]
    assert "wrist_pitch.pos" in restored or "base_pitch.pos" in restored, restored


def test_a_low_confidence_bearing_is_not_used_to_seed_the_sweep():
    """An estimate about to be dropped is not an ordering hint."""
    from hal.drivers.tracking import aim

    res, svc = _run(bearing=120.0, confidence=aim.MIN_BEARING_CONFIDENCE - 0.05)
    seeded_from_bearing = [h for h in svc.holds if h.get("base_yaw.pos") == 120.0]
    assert seeded_from_bearing == [], "a bearing below the floor must not aim the head"
    assert res.looks_visited >= 1


def test_no_bearing_rests_on_the_idle_pose_before_sweeping():
    """With no bearing, the sweep starts from the idle pose."""
    _res, svc = _run(bearing=None)
    rested = [h for h in svc.holds if h.get("base_pitch.pos") == 29.8]
    assert rested, f"expected a rest on the idle pose, got {svc.holds[:3]}"


def test_with_no_idle_pose_either_the_sweep_starts_where_it_stands():
    """With no memory at all, the device still sweeps."""
    _res, svc = _run(bearing=None, idle_baseline={})
    idle_pitch = _FakeSvc.IDLE_BASELINE["base_pitch.pos"]
    assert [h for h in svc.holds if h.get("base_pitch.pos") == idle_pitch] == []


def test_a_confident_bearing_still_seeds_the_sweep():
    from hal.drivers.tracking import aim

    _res, svc = _run(bearing=40.0, confidence=aim.MIN_BEARING_CONFIDENCE + 0.05)
    assert svc.holds, "a bearing above the floor should be used"


def test_a_failed_posture_restore_still_sweeps():
    """Sweeping from the wrong pitch beats not sweeping at all."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    svc = _FakeSvc()
    # Fail only the FIRST move — the posture restore.
    _real_move = svc.move_and_hold
    _calls = {"n": 0}

    def _restore_fails(target, duration=None):
        _calls["n"] += 1
        if _calls["n"] == 1:
            raise RuntimeError("servo busy")
        return _real_move(target, duration=duration)

    svc.move_and_hold = mock.Mock(side_effect=_restore_fails)
    det = mock.Mock()
    det.detect = mock.Mock(return_value=None)
    est = mock.Mock(bearing_deg=40.0, confidence=0.9)
    est.pose = {"base_yaw.pos": 40.0, "wrist_pitch.pos": -30.0}

    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch.object(search.time, "sleep"),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=est),
    ):
        res = search.search_for_subject(detector=det)

    assert res.looks_visited >= 1, "a failed restore must not abort the search"


def _rolls(svc):
    """Every wrist_roll angle the sweep commanded, in order."""
    return [h["wrist_roll.pos"] for h in svc.holds if "wrist_roll.pos" in h]


def _sweep_rolls(svc):
    """Only the sweep's looks, excluding the restore and home moves."""
    return _rolls(svc)[1:-1]


def test_each_bearing_walks_the_look_ring():
    """Each bearing turns the head through its look ring at a fixed body."""
    _res, svc = _run(bearing=None)
    rolls = _rolls(svc)
    want = [r for r, _ in search.LOOK_CIRCLE[:search.HALF_LOOKS]]

    first = rolls.index(want[0])
    assert rolls[first:first + len(want)] == want, (
        f"expected the half ring {want}, got {rolls[first:first + len(want)]}"
    )
    assert want[-1] == search.ROLL_LOOK_DEG, "the half ring must end on the right"


def test_the_ring_only_ever_moves_the_wrist():
    """Pitch tiers move the head only, never reshaping the arm."""
    _res, svc = _run(bearing=None)
    # Only the LOOK moves; a look always commands wrist_roll.
    looks = svc.holds[1:-1]
    assert looks, "no looks were commanded"
    for h in looks:
        for forbidden in ("base_pitch.pos", "elbow_pitch.pos"):
            assert forbidden not in h, (
                f"{forbidden} moved during a look — only the wrist may look around"
            )


def _view_directions(svc):
    """Where the camera pointed after each commanded move: base_yaw + wrist_roll."""
    return [yaw + roll for yaw, roll in svc.trail]


def test_the_view_carries_on_from_stop_to_stop():
    """One stop ends where the next begins, so the camera sweeps continuously."""
    _res, svc = _run(bearing=None)
    views = _view_directions(svc)[1:-1]

    jumps = [abs(b - a) for a, b in zip(views, views[1:])
             if abs(b - a) > search.STEP_DEG + 1e-6]
    assert len(jumps) <= 1, (
        f"more than one discontinuity in {[round(v) for v in views]}")


def test_the_handover_onto_the_next_bearing_is_a_short_step():
    """The next bearing continues in the direction of travel."""
    _res, svc = _run(bearing=None)
    views = _view_directions(svc)[1:]

    end_of_seed = views[search.HALF_LOOKS - 1]
    after_the_turn = views[search.HALF_LOOKS]
    step = after_the_turn - end_of_seed
    assert 0 < step <= search.STEP_DEG / 2 + 1e-6, (
        f"handover stepped {step:+.0f} deg ({end_of_seed:+.0f} -> "
        f"{after_the_turn:+.0f}) in {[round(v) for v in views[:8]]}"
    )


def test_a_subject_found_mid_look_stops_the_sweep_there():
    """The sweep still stops on the first subject seen."""
    res, svc = _run(detect_at_stop=2, bearing=None)
    assert res.found
    assert res.looks_visited == 2, "it kept looking after finding someone"


def test_looking_around_multiplies_the_stops_not_the_yaw_positions():
    """Three looks per yaw stop."""
    res, svc = _run(bearing=None)
    yaw_stops = len(search._stop_list(_FakeSvc.IDLE_BASELINE["base_yaw.pos"]))
    assert res.looks_visited == yaw_stops * search.HALF_LOOKS, (
        f"{res.looks_visited} stops from {yaw_stops} yaw positions"
    )
    assert yaw_stops == 3, "three yaw positions is the whole point of the wider step"


def test_a_failed_sweep_returns_to_where_it_started():
    """A failed sweep returns the head home."""
    _res, svc = _run(bearing=None)
    last = svc.holds[-1]
    assert last.get("wrist_roll.pos") == pytest.approx(
        _FakeSvc.IDLE_BASELINE["wrist_roll.pos"]
    ), f"did not return to the starting pose: {last}"


def test_a_successful_sweep_keeps_looking_at_the_subject():
    """A successful sweep stays on the subject instead of returning home."""
    # Find on the SECOND look; the ring opens at centre where straightening proves nothing.
    res, svc = _run(detect_at_stop=2, bearing=None)
    assert res.found
    last = svc.holds[-1]
    assert last.get("wrist_roll.pos") == pytest.approx(0.0), "head left cocked"
    assert last["base_yaw.pos"] == pytest.approx(
        _FakeSvc.IDLE_BASELINE["base_yaw.pos"] + search.LOOK_CIRCLE[1][0]
    )


def test_an_abort_also_returns_to_where_it_started():
    """An abort returns the head to where the sweep started."""
    res, svc = _run(abort_at_stop=2, bearing=None)
    assert res.reason == "aborted"
    last = svc.holds[-1]
    assert last.get("wrist_roll.pos") == pytest.approx(
        _FakeSvc.IDLE_BASELINE["wrist_roll.pos"]
    ), f"an aborted sweep did not return to its starting pose: {last}"


def test_the_shutter_waits_for_the_arm_to_stop_moving():
    """The sweep waits for arrival, not just for move_and_hold to return."""
    svc = _FakeSvc()
    reads = {"n": 0}
    real = svc.get_positions

    def still_moving():
        # Reports a different yaw for the first few polls, like an arm still moving.
        reads["n"] += 1
        pose = dict(real())
        if reads["n"] < 4:
            pose["base_yaw.pos"] = pose["base_yaw.pos"] + 30.0 / reads["n"]
        return pose

    svc.get_positions = still_moving
    t0 = time.monotonic()
    search._wait_until_still(svc, {"base_yaw.pos": 90.0})
    waited = time.monotonic() - t0

    assert reads["n"] >= 4, "it did not keep polling while the arm moved"
    assert waited < search.ARRIVE_TIMEOUT_S, "it waited out the whole timeout"


def test_waiting_gives_up_rather_than_stalling_the_sweep():
    """An unreachable stop times out and is shot from anyway."""
    svc = _FakeSvc()
    jitter = {"n": 0}

    def never_settles():
        jitter["n"] += 1
        return {"base_yaw.pos": 100.0 * (jitter["n"] % 2), "wrist_roll.pos": 0.0}

    svc.get_positions = never_settles
    original = search.ARRIVE_TIMEOUT_S
    search.ARRIVE_TIMEOUT_S = 0.4
    try:
        t0 = time.monotonic()
        search._wait_until_still(svc, {"base_yaw.pos": 90.0})
        assert time.monotonic() - t0 < 2.0, "it stalled instead of giving up"
    finally:
        search.ARRIVE_TIMEOUT_S = original


def test_the_sweep_owns_the_body_for_its_whole_duration():
    """The sweep owns the body for its whole duration."""
    import hal.app_state as app_state
    from hal.drivers.tracking import aim

    owned_during = []
    real_grab = search._grab_frame

    svc = _FakeSvc()
    svc._tracking_active = False

    def watching(cap):
        owned_during.append(getattr(svc, "_tracking_active", False))
        return real_grab(cap)

    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    det = mock.Mock()
    det.detect = mock.Mock(return_value=None)

    with mock.patch.object(app_state, "camera_capture", mock.Mock(), create=True), \
         mock.patch.object(app_state, "animation_service", svc, create=True), \
         mock.patch.object(app_state, "safety_policy", None, create=True), \
         mock.patch.object(search, "_grab_frame", watching), \
         mock.patch.object(search, "_detect_subject", lambda d, f: (None, None, None)):
        search.search_for_subject(detector=det)

    assert owned_during, "the sweep never looked at anything"
    assert all(owned_during), "idle was free to overwrite the sweep's own stops"
    assert not svc._tracking_active, "ownership was not handed back"


def test_a_caller_can_follow_the_sweep_stop_by_stop():
    """A caller can follow the sweep stop by stop."""
    seen = []
    _res, _svc = _run(bearing=None, on_progress=lambda v, t: seen.append((v, t)))

    assert seen, "the sweep reported no progress at all"
    assert [v for v, _ in seen] == list(range(1, len(seen) + 1)), seen
    assert all(t == seen[-1][0] for _, t in seen), "the total kept changing"


def test_the_midpoint_of_a_full_sweep_is_the_middle_look():
    """Look #5 is the middle look of the middle stop."""
    seen = []
    _res, _svc = _run(bearing=None, on_progress=lambda v, t: seen.append((v, t)))

    total = seen[-1][1]
    halfway = [v for v, t in seen if v * 2 >= t][0]
    expected = 3 * search.HALF_LOOKS
    assert total == expected, f"expected {expected} looks, got {total}"
    assert halfway == expected // 2, f"midpoint should be look {expected // 2}, got {halfway}"


def test_a_talkative_caller_cannot_sink_the_sweep():
    def boom(visited, total):
        raise RuntimeError("tts exploded")

    res, _svc = _run(bearing=None, on_progress=boom)
    expected = 3 * search.HALF_LOOKS
    assert res.looks_visited == expected, "the sweep stopped when the callback threw"


def test_every_sweep_narrates_its_own_midpoint():
    """Every sweep narrates its own midpoint."""
    said = []
    with mock.patch("hal.drivers.tracking.aim._say", side_effect=said.append):
        _res, _svc = _run(bearing=None)

    assert said == ["look_still_searching"], (
        f"expected exactly one midpoint phrase, got {said}"
    )


def test_a_sweep_that_ends_early_stays_quiet():
    """Found on the second look: there was no long silence to fill."""
    said = []
    with mock.patch("hal.drivers.tracking.aim._say", side_effect=said.append):
        res, _svc = _run(detect_at_stop=2, bearing=None)

    assert res.found
    assert said == [], f"narrated a sweep that never went quiet: {said}"


def test_a_caller_can_take_over_the_narration():
    """The default narrates; a custom handler replaces it."""
    said = []
    seen = []
    with mock.patch("hal.drivers.tracking.aim._say", side_effect=said.append):
        _res, _svc = _run(bearing=None, on_progress=lambda v, t: seen.append(v))

    assert seen, "the caller's handler never ran"
    assert said == [], "the default narration fired as well as the caller's"


def test_the_sweep_speeds_the_base_up_only_for_itself():
    """The base has to be brisk during a sweep and unchanged outside it."""
    _res, svc = _run(bearing=None)

    assert svc.speeds, "the sweep never touched the base speed"
    assert svc.speeds[0] == ("base_yaw", search.SWEEP_YAW_SPEED)
    assert svc.speeds[-1][0] == "base_yaw"
    assert svc.speeds[-1][1] == _FakeSvc.UNWRITTEN_SPEED_EQUIVALENT, (
        "the sweep left its own cap behind instead of the resting value"
    )


def test_the_base_speed_is_restored_even_when_the_sweep_raises():
    """A search that dies part-way must not leave the arm retuned behind it."""
    svc = _FakeSvc()
    with mock.patch.object(search, "_sweep", side_effect=RuntimeError("boom")), \
         mock.patch.object(state, "camera_capture", mock.Mock(), create=True), \
         mock.patch.object(state, "animation_service", svc, create=True), \
         mock.patch.object(state, "safety_policy", None, create=True):
        with pytest.raises(RuntimeError):
            search.search_for_subject(detector=mock.Mock())

    assert len(svc.speeds) == 2, f"speed not restored: {svc.speeds}"
    assert svc.speeds[-1][1] == _FakeSvc.UNWRITTEN_SPEED_EQUIVALENT


def test_the_resting_speed_the_sweep_restores_is_the_one_startup_writes():
    """The killed and clean sweep paths share one resting pace."""
    pytest.importorskip("lerobot")
    from hal.drivers.motors.animation_service import AnimationService

    assert AnimationService._SERVO_REST_SPEED.get(1) == _FakeSvc.UNWRITTEN_SPEED_EQUIVALENT


def _run_target(target="person", exhaustive=False, hits=(), person_everywhere=False):
    """A sweep whose detector answers per-target."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    svc = _FakeSvc()
    n = {"person": 0, "obj": 0}

    def _detect(f, t, strict=True, **kw):
        if t == "person":
            n["person"] += 1
            return (300, 100, 40, 200) if person_everywhere else None
        if t == "face":
            return None
        n["obj"] += 1
        return (10, 10, 20, 20) if n["obj"] in hits else None

    det = mock.Mock()
    det.detect = mock.Mock(side_effect=_detect)
    with (
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch.object(search.time, "sleep"),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=None),
    ):
        res = search.search_for_subject(target=target, detector=det,
                                        on_progress=lambda *_: None,
                                        exhaustive=exhaustive)
    return res, svc, det


def test_an_object_search_is_not_ended_by_a_person():
    """An object search passes its target to the detector and is not ended by a person."""
    res, _svc, det = _run_target(target="keyboard", hits=(), person_everywhere=True)
    assert res.found is False, "a person ended a keyboard search"
    assert res.reason == "no keyboard found"
    asked = {c.args[1] for c in det.detect.call_args_list}
    assert asked == {"keyboard"}, f"probed for {asked}, not just the keyboard"


def test_an_object_search_finds_the_object():
    res, _svc, _det = _run_target(target="keyboard", hits=(4,))
    assert res.found is True
    assert res.reason == "found keyboard"


def test_a_person_search_keeps_the_closest_person_policy():
    """Person targets keep the closest-person policy; object targets do not."""
    res, _svc, det = _run_target(target="person", person_everywhere=True)
    assert res.found is True
    assert "person" in res.reason
    assert any(c.args[1] == "person" for c in det.detect.call_args_list)


def test_the_sweep_tilts_as_well_as_pans():
    """The sweep also tilts to cover the desk below the horizon."""
    _res, svc, _det = _run_target(target="person")
    pitched = [h["wrist_pitch.pos"] for h in svc.holds if "wrist_pitch.pos" in h]
    assert pitched, "the sweep never commanded wrist_pitch"
    assert max(pitched) - min(pitched) >= search.PITCH_LOOK_DEG - 1e-6, (
        f"the ring spanned only {max(pitched) - min(pitched):.1f} deg of pitch")


def test_a_full_scan_does_not_stop_at_the_first_hit():
    """Exhaustive mode does not stop on the first person."""
    quick, _svc, _det = _run_target(target="person", person_everywhere=True)
    full, _svc2, _det2 = _run_target(target="person", person_everywhere=True,
                                     exhaustive=True)
    assert quick.looks_visited == 1, "the quick sweep should stop at the first hit"
    assert full.looks_visited == 3 * len(search.LOOK_CIRCLE), (
        "the full sweep stopped early")
    assert full.found is True
    assert "x" in full.reason, f"expected a sighting count, got {full.reason!r}"


def test_a_full_scan_adds_the_upper_half_of_the_ring():
    """Only exhaustive mode looks above the horizon."""
    half = search.LOOK_CIRCLE[:search.HALF_LOOKS]
    assert all(dp >= 0 for _r, dp in half), "the default sweep must not look up"
    assert any(dp < 0 for _r, dp in search.LOOK_CIRCLE), "nothing looks up at all"
    assert len(search.LOOK_CIRCLE) > search.HALF_LOOKS


def test_a_hit_is_centred_before_the_sweep_returns():
    """A hit is centred on the object, not the stop's look direction (#342)."""
    calls = []

    def _fake_centre(svc, cap, probe, deadline_s=None):
        from hal.drivers.tracking.aim import CentreResult
        calls.append(True)
        return CentreResult(True, "centred", 1, 12.0, 0.01,
                            (300, 200, 40, 40), object())

    with mock.patch("hal.drivers.tracking.aim.centre_on_box", _fake_centre):
        res, _svc, _det = _run_target(target="keyboard", hits=(1,))

    assert calls, "a hit must run the centring correction"
    assert res.found is True
    assert res.centred is True
    assert res.box == (300, 200, 40, 40), "the result must carry the centred box"


def test_a_failed_centring_still_reports_the_find():
    """Losing the box during correction still reports a find."""
    def _fake_centre(svc, cap, probe, deadline_s=None):
        from hal.drivers.tracking.aim import CentreResult
        return CentreResult(False, "lost the subject", 2, 8.0, 0.4, None, None)

    with mock.patch("hal.drivers.tracking.aim.centre_on_box", _fake_centre):
        res, _svc, _det = _run_target(target="keyboard", hits=(1,))

    assert res.found is True, "a wobbly correction must not erase the find"
    assert res.centred is False


def test_the_probe_handed_to_the_centring_loop_looks_for_the_TARGET():
    """The correction chases the requested target."""
    probes = []

    def _fake_centre(svc, cap, probe, deadline_s=None):
        from hal.drivers.tracking.aim import CentreResult
        import numpy as np
        probes.append(probe(np.zeros((480, 640, 3), dtype=np.uint8)))
        return CentreResult(True, "centred", 1, 1.0, 0.0, (1, 2, 3, 4), None)

    with mock.patch("hal.drivers.tracking.aim.centre_on_box", _fake_centre):
        _res, _svc, det = _run_target(target="keyboard", hits=(1, 2))

    assert probes, "the centring loop was given no probe"
    asked = {c.args[1] for c in det.detect.call_args_list}
    assert asked == {"keyboard"}, f"the probe went looking for {asked}"


def test_an_exhaustive_sweep_counts_bearings_and_looks_apart():
    """Looks and stops are reported as separate counts (#342)."""
    res, _svc, _det = _run_target(target="person", person_everywhere=True,
                                  exhaustive=True)

    assert res.looks_visited == 3 * len(search.LOOK_CIRCLE)
    assert res.bearings_visited == 3
    assert res.looks_visited != res.bearings_visited, (
        "looks and bearings are different quantities")


def test_the_winning_frame_is_written_where_the_agent_can_read_it(tmp_path):
    """The found image lands in the active runtime's snapshot dir (#342)."""
    def _fake_centre(svc, cap, probe, deadline_s=None):
        from hal.drivers.tracking.aim import CentreResult
        return CentreResult(True, "centred", 1, 5.0, 0.01, (10, 10, 20, 20),
                            np.zeros((480, 640, 3), dtype=np.uint8))

    with (
        mock.patch.object(state, "_SNAPSHOT_DIR", str(tmp_path), create=True),
        mock.patch.object(state, "_snapshot_paths", [], create=True),
        mock.patch("hal.drivers.tracking.aim.centre_on_box", _fake_centre),
    ):
        res, _svc, _det = _run_target(target="keyboard", hits=(1,))

    assert res.image_path is not None, "a find with no image is not showable"
    assert res.image_path.startswith(str(tmp_path))
    assert os.path.exists(res.image_path)
    assert os.path.getsize(res.image_path) > 0


def test_a_miss_writes_no_image():
    """A failed search writes no image."""
    res, _svc, _det = _run_target(target="keyboard", hits=())
    assert res.found is False
    assert res.image_path is None


def test_a_find_always_has_an_image_even_when_centring_never_got_a_frame(tmp_path):
    """Falls back to the sweep's frame and box when centring never grabs."""
    def _fake_centre(svc, cap, probe, deadline_s=None):
        from hal.drivers.tracking.aim import CentreResult
        return CentreResult(False, "no fresh frame", 0, 0.0, None, None, None)

    with (
        mock.patch.object(state, "_SNAPSHOT_DIR", str(tmp_path), create=True),
        mock.patch.object(state, "_snapshot_paths", [], create=True),
        mock.patch("hal.drivers.tracking.aim.centre_on_box", _fake_centre),
    ):
        res, _svc, _det = _run_target(target="keyboard", hits=(1,))

    assert res.found is True
    assert res.centred is False
    assert res.image_path is not None, "a find with no image to show"
    assert os.path.exists(res.image_path)


def test_the_saved_image_carries_the_box_and_not_the_aim_debug_lines(tmp_path):
    """The search image has the box but no centre lines."""
    seen = {}

    def _spy(frame, box=None, label="", both_axes=False, centre_lines=True):
        seen.update(box=box, label=label, centre_lines=centre_lines)
        return b"\xff\xd8jpeg"

    def _fake_centre(svc, cap, probe, deadline_s=None):
        from hal.drivers.tracking.aim import CentreResult
        return CentreResult(True, "centred", 1, 5.0, 0.01, (10, 10, 20, 20),
                            np.zeros((480, 640, 3), dtype=np.uint8))

    with (
        mock.patch.object(state, "_SNAPSHOT_DIR", str(tmp_path), create=True),
        mock.patch.object(state, "_snapshot_paths", [], create=True),
        mock.patch("hal.drivers.tracking.aim.centre_on_box", _fake_centre),
        mock.patch("hal.drivers.tracking.look_debug.encode_annotated", _spy),
    ):
        _run_target(target="keyboard", hits=(1,))

    assert seen["box"] == (10, 10, 20, 20), "the box must be drawn"
    assert seen["centre_lines"] is False, "aim-debug lines in a user-facing image"
    assert seen["label"] == "keyboard", (
        f"the caption should name the thing, got {seen['label']!r}")


def test_the_snapshot_pool_is_rotated_like_the_camera_route(tmp_path):
    """Search images rotate within the same capped pool."""
    def _fake_centre(svc, cap, probe, deadline_s=None):
        from hal.drivers.tracking.aim import CentreResult
        return CentreResult(True, "centred", 1, 5.0, 0.01, (10, 10, 20, 20),
                            np.zeros((480, 640, 3), dtype=np.uint8))

    kept = [str(tmp_path / f"old_{i}.jpg") for i in range(state._SNAPSHOT_MAX)]
    for p in kept:
        open(p, "wb").write(b"x")

    with (
        mock.patch.object(state, "_SNAPSHOT_DIR", str(tmp_path), create=True),
        mock.patch.object(state, "_snapshot_paths", list(kept), create=True),
        mock.patch("hal.drivers.tracking.aim.centre_on_box", _fake_centre),
    ):
        res, _svc, _det = _run_target(target="keyboard", hits=(1,))
        assert len(state._snapshot_paths) == state._SNAPSHOT_MAX
        assert res.image_path in state._snapshot_paths
        assert not os.path.exists(kept[0]), "the oldest snapshot was not removed"


def _search_client():
    """A TestClient over the servo router — no hardware, no app startup."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hal.routes.servo import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_the_route_reports_bearings_and_looks_separately():
    """The route reports looks and stops separately (#342)."""
    hit = search.SearchResult(True, "found keyboard", 7, 85.0, -45.0, 2,
                              "keyboard", (1, 2, 3, 4), True, "/tmp/snap_1.jpg")
    with mock.patch("hal.drivers.tracking.search.search_for_subject",
                    return_value=hit):
        body = _search_client().post("/servo/search",
                                     json={"target": "keyboard"}).json()

    assert body["looks_visited"] == 7
    assert body["bearings_visited"] == 2
    assert "7 look(s)" in body["message"] and "2 bearing(s)" in body["message"]
    assert "stop(s)" not in body["message"], "looks are not stops"


def test_the_route_carries_every_field_the_agent_needs():
    """The response model declares every SearchResult field."""
    hit = search.SearchResult(True, "found keyboard", 7, 85.0, -45.0, 2,
                              "keyboard", (1, 2, 3, 4), True, "/tmp/snap_1.jpg")
    with mock.patch("hal.drivers.tracking.search.search_for_subject",
                    return_value=hit):
        body = _search_client().post("/servo/search",
                                     json={"target": "keyboard"}).json()

    assert body["found"] is True
    assert body["target"] == "keyboard"
    assert body["kind"] == "keyboard"
    assert body["found_at_yaw"] == 85.0
    assert body["found_at_roll"] == -45.0
    assert body["centred"] is True
    assert body["image_path"] == "/tmp/snap_1.jpg"


def test_the_route_answers_a_miss_without_an_image():
    """A not-found result is a clean answer with no image."""
    miss = search.SearchResult(False, "no keyboard found", 18,
                               bearings_visited=3)
    with mock.patch("hal.drivers.tracking.search.search_for_subject",
                    return_value=miss):
        resp = _search_client().post("/servo/search", json={"target": "keyboard"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is False
    assert body["image_path"] is None
    assert body["message"] == "no keyboard found after 18 look(s) across 3 bearing(s)"


def test_an_empty_body_still_searches_for_a_person():
    """The historical caller sends nothing at all."""
    hit = search.SearchResult(True, "found person", 1, 5.0, 0.0, 1, "person")
    with mock.patch("hal.drivers.tracking.search.search_for_subject",
                    return_value=hit) as sweep:
        body = _search_client().post("/servo/search").json()

    assert sweep.call_args.kwargs["target"] == "person"
    assert body["target"] == "person"


def test_the_correction_runs_before_the_head_is_straightened():
    """Centring starts from the pose the object was found at."""
    order = []

    def _fake_centre(svc, cap, probe, deadline_s=None):
        from hal.drivers.tracking.aim import CentreResult
        order.append("centre")
        return CentreResult(True, "centred", 1, 10.0, 0.0, (300, 200, 40, 40), object())

    def _fake_straighten(svc, yaw, roll):
        order.append("straighten")

    with (
        mock.patch("hal.drivers.tracking.aim.centre_on_box", _fake_centre),
        mock.patch.object(search, "_straighten_head_onto", _fake_straighten),
    ):
        _run_target(target="keyboard", hits=(1,))

    assert order == ["centre", "straighten"], order


def test_an_object_search_stops_at_the_first_sighting_even_when_told_to_be_exhaustive():
    """A target search stops at the first sighting even when exhaustive."""
    res, svc, det = _run_target(target="doll", exhaustive=True, hits=(2,))

    assert res.found is True
    assert res.looks_visited == 2, "kept sweeping after the first sighting"
    assert "x" not in res.reason, f"reported a survey count for a find: {res.reason!r}"
    assert svc.holds[-1] != _FakeSvc.IDLE_BASELINE, "went home instead of staying on the doll"
    assert any(c.args[1] == "doll" for c in det.detect.call_args_list[2:]), (
        "no probe after the sighting — the correction never ran")


def test_exhaustive_still_surveys_for_people():
    """The survey mode keeps its job: everyone in the room, then home."""
    res, _svc, _det = _run_target(target="person", person_everywhere=True, exhaustive=True)
    assert res.found is True
    assert res.looks_visited == 3 * len(search.LOOK_CIRCLE)
    assert "x" in res.reason


def test_the_centring_probe_sticks_to_the_instance_the_sweep_found():
    """The probe follows the anchored instance among multiple candidates."""
    from hal.drivers.tracking.search import _sticky_probe

    det = mock.Mock()
    det.detect_candidates = mock.Mock(return_value=[
        ((40, 300, 120, 60), 0.91),
        ((520, 220, 130, 70), 0.62),
    ])
    det.detect = mock.Mock(return_value=(40, 300, 120, 60))

    probe = _sticky_probe(det, "keyboard", first_box=(500, 200, 130, 70))
    box = probe(np.zeros((480, 640, 3), dtype=np.uint8))

    assert box == (520, 220, 130, 70), f"probe jumped to the higher-confidence instance: {box}"
    det.detect.assert_not_called()


def test_the_sticky_probe_follows_its_instance_as_the_camera_turns():
    """The anchor moves with the object after each correction."""
    from hal.drivers.tracking.search import _sticky_probe

    det = mock.Mock()
    frames = [
        [((500, 200, 100, 60), 0.7), ((40, 300, 120, 60), 0.9)],
        [((380, 210, 100, 60), 0.7), ((40, 300, 120, 60), 0.9)],
        [((330, 215, 100, 60), 0.7), ((40, 300, 120, 60), 0.9)],
    ]
    det.detect_candidates = mock.Mock(side_effect=lambda f, t, **kw: frames.pop(0))
    probe = _sticky_probe(det, "keyboard", first_box=(500, 200, 100, 60))
    f = np.zeros((480, 640, 3), dtype=np.uint8)

    assert probe(f) == (500, 200, 100, 60)
    assert probe(f) == (380, 210, 100, 60)
    assert probe(f) == (330, 215, 100, 60)


def test_the_sticky_probe_falls_back_to_detect_for_targets_without_candidates():
    """Open-vocab and person targets both work without candidate lists."""
    from hal.drivers.tracking.search import _sticky_probe

    det = mock.Mock()
    det.detect_candidates = mock.Mock(return_value=[])
    det.detect = mock.Mock(return_value=(300, 200, 40, 40))
    probe = _sticky_probe(det, "unicorn", first_box=(300, 200, 40, 40))
    assert probe(np.zeros((480, 640, 3), dtype=np.uint8)) == (300, 200, 40, 40)


def test_the_sticky_probe_refuses_a_candidate_that_could_not_be_the_same_object():
    """A candidate too far from the anchor is rejected."""
    from hal.drivers.tracking.search import _sticky_probe

    det = mock.Mock()
    det.detect_candidates = mock.Mock(return_value=[((40, 300, 120, 60), 0.91)])
    probe = _sticky_probe(det, "keyboard", first_box=(500, 200, 130, 70))

    assert probe(np.zeros((480, 640, 3), dtype=np.uint8)) is None


def test_the_sticky_probe_refuses_a_candidate_that_moved_away_from_centre():
    """A candidate on the wrong side of the move direction is rejected."""
    from hal.drivers.tracking.search import _sticky_probe

    f = np.zeros((480, 640, 3), dtype=np.uint8)
    det = mock.Mock()
    # anchor centre x = 190 -> dx -20%. Candidate centre x = 62 -> dx -40%.
    det.detect_candidates = mock.Mock(return_value=[((32, 220, 60, 40), 0.9)])
    probe = _sticky_probe(det, "keyboard", first_box=(160, 220, 60, 40))
    assert probe(f) is None, "accepted a candidate that moved away from centre"

    # Overshoot: same object corrected past the middle; must be accepted.
    det.detect_candidates = mock.Mock(return_value=[((450, 220, 60, 40), 0.9)])
    probe = _sticky_probe(det, "keyboard", first_box=(160, 220, 60, 40))
    assert probe(f) == (450, 220, 60, 40)


# After a hit the sweep hands the body back to idle after a window.
def test_a_find_hands_the_body_back_after_the_hold_window():
    with mock.patch("hal.drivers.tracking.body.release_to_idle_later") as later:
        res, svc = _run(detect_at_stop=2)
    assert res.found is True
    later.assert_called_once()
    delay, reason = later.call_args[0][0], later.call_args[0][1]
    assert delay == search.body.HOLD_AFTER_FIND_S
    assert "search" in reason


def test_a_miss_hands_the_body_back_immediately():
    with mock.patch("hal.drivers.tracking.body.release_to_idle_later") as later:
        res, _ = _run(detect_at_stop=None)
    assert res.found is False
    later.assert_called_once()
    assert later.call_args[0][0] == 0.0


def test_no_handback_when_the_sweep_never_took_the_body():
    with mock.patch("hal.drivers.tracking.body.release_to_idle_later") as later:
        _run(disabled=True)
    later.assert_not_called()


_NEAR = config.GAZE_BEARING_MIN_FACE_HEIGHT_FRAC + 0.05


def _user_run(tracks_at_look, target="person"):
    """tracks_at_look: {1-based look number: [FaceTrack, ...]}; other looks see no face.

    A body is in view at every look, so only the user check can end the sweep.
    """
    looks = {"n": 0}

    def fake_observe(grab, *a, **kw):
        looks["n"] += 1
        return tracks_at_look.get(looks["n"], [])

    centred = []

    def fake_centre(svc, cap, probe):
        centred.append(probe)
        return CentreResult(True, "ok")

    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    svc = _FakeSvc()
    det = mock.Mock()
    det.detect = mock.Mock(return_value=(300, 100, 40, 200))
    with (
        mock.patch.object(user_check, "observe_faces", side_effect=fake_observe) as obs,
        mock.patch("hal.drivers.tracking.aim.centre_on_box", side_effect=fake_centre),
        mock.patch.object(search, "_persist_hit", return_value=None),
        mock.patch.object(state, "camera_capture", _FakeCap(frame)),
        mock.patch.object(state, "animation_service", svc),
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "_camera_disabled", False, create=True),
        mock.patch.object(search.time, "sleep"),
        mock.patch("hal.drivers.tracking.user_bearing.read_estimate", return_value=None),
    ):
        res = search.search_for_subject(target=target, detector=det,
                                        for_user=(target == "person"))
    return res, obs, centred, svc


def test_a_user_sweep_never_stops_on_a_body_alone():
    """#545: bodies at every look, no qualifying face -> not found."""
    res, _obs, centred, _svc = _user_run({})
    assert not res.found
    assert centred == [], "it centred on somebody who never passed the user check"


def test_a_user_sweep_rejects_a_side_on_face():
    side_on = FaceTrack((300, 100, 60, 60),
                        FaceEvidence(_NEAR, facing_ratio=0.0, facing_samples=6))
    res, _obs, centred, _svc = _user_run({1: [side_on]})
    assert not res.found
    assert centred == []


def test_a_user_sweep_stops_on_a_face_that_passes():
    facing = FaceTrack((300, 100, 60, 60),
                       FaceEvidence(_NEAR, facing_ratio=1.0, facing_samples=6))
    res, _obs, centred, _svc = _user_run({2: [facing]})
    assert res.found and res.kind == "face"
    assert len(centred) == 1


def test_a_failed_user_sweep_returns_to_where_it_started():
    res, _obs, _centred, svc = _user_run({})
    assert not res.found
    last = svc.holds[-1]
    assert last.get("wrist_roll.pos") == pytest.approx(
        _FakeSvc.IDLE_BASELINE["wrist_roll.pos"]
    ), f"did not return to the starting pose: {last}"


def test_an_object_search_never_runs_the_user_check():
    _res, obs, _centred, _svc = _user_run({}, target="cup")
    obs.assert_not_called()


def test_the_default_sweep_still_stops_on_a_body():
    """/servo/search and look-aim's own sweep keep today's behaviour."""
    res, _svc = _run(detect_at_stop=1)
    assert res.found and res.kind == "person"


def _probe_on(faces, anchor):
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    with (
        mock.patch("hal.drivers.tracking.frame_utils.downscale", lambda f: (f, 1.0)),
        mock.patch("hal.drivers.tracking.detection.detect_faces_with_landmarks",
                   return_value=[(b, ()) for b in faces]),
    ):
        return search._user_face_probe(anchor)(frame)


def test_the_user_probe_keeps_the_face_it_was_given():
    user, coworker = (300, 100, 60, 60), (40, 120, 90, 90)
    assert _probe_on([coworker, user], anchor=(290, 110, 60, 60)) == user


def test_the_user_probe_never_jumps_to_another_face():
    far_away = (600, 400, 30, 30)
    assert _probe_on([far_away], anchor=(40, 20, 60, 60)) is None


def _look_pitches(svc):
    """wrist_pitch of every look the sweep made (looks always carry wrist_roll)."""
    return {round(h["wrist_pitch.pos"], 1) for h in svc.holds
            if "wrist_roll.pos" in h and "wrist_pitch.pos" in h}


_SEATED = _FakeSvc.IDLE_BASELINE["wrist_pitch.pos"]


def test_the_user_sweep_looks_up_never_down():
    """Faces are at or above seated height: a standing user was missed by the down looks."""
    res, _obs, _centred, svc = _user_run({})
    pitches = _look_pitches(svc)
    assert round(_SEATED - search.PITCH_LOOK_DEG, 1) in pitches, f"never looked up: {pitches}"
    assert round(_SEATED + search.PITCH_LOOK_DEG, 1) not in pitches, f"looked down: {pitches}"


def test_the_user_sweep_keeps_six_looks_per_stop():
    res, _obs, _centred, _svc = _user_run({})
    assert res.looks_visited == 3 * search.HALF_LOOKS


def test_the_other_sweeps_still_look_down_at_the_desk():
    """/servo/search ("find my things") and look-aim's fallback sweep are unchanged."""
    _res, svc = _run(bearing=None)
    pitches = _look_pitches(svc)
    assert round(_SEATED + search.PITCH_LOOK_DEG, 1) in pitches, f"never looked down: {pitches}"
    assert round(_SEATED - search.PITCH_LOOK_DEG, 1) not in pitches, f"looked up: {pitches}"


def test_the_up_pattern_is_the_down_pattern_mirrored():
    assert search.USER_LOOK_CIRCLE == tuple(
        (roll, -dp) for roll, dp in search.LOOK_CIRCLE[:search.HALF_LOOKS]
    )
