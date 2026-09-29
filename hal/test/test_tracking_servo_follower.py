"""Focused behavior tests for the tracking servo follower."""

import threading
import time

import pytest

from hal.drivers.tracking import constants as C
from hal.drivers.tracking import servo_follow
from hal.drivers.tracking.servo_follow import JOINTS, ServoFollower
from test.body_ownership import BodyOwnership


def _pose(value: float) -> dict[str, float]:
    return {joint: value for joint in JOINTS}


class _FakeRobot:
    def __init__(self):
        self.actions: list[dict[str, float]] = []

    def send_action(self, action: dict[str, float]) -> None:
        self.actions.append(dict(action))


class _FakeAnimationService:
    def __init__(self):
        self.bus_lock = threading.RLock()
        self.is_frozen = False
        self.robot = _FakeRobot()


def test_hold_retargets_the_current_pose_instead_of_leaving_a_stale_goal():
    follower = ServoFollower()
    follower.set_goal(_pose(10.0))
    follower._yaw = 1.0
    follower._base_pitch = 2.0
    follower._elbow_pitch = 3.0
    follower._wrist_pitch = 4.0

    follower.hold()

    assert follower._goal == {
        "base_yaw.pos": 1.0,
        "base_pitch.pos": 2.0,
        "elbow_pitch.pos": 3.0,
        "wrist_pitch.pos": 4.0,
    }


def test_follower_uses_elapsed_time_and_bounds_scheduler_stalls():
    assert ServoFollower._tick_dt(10.040, 10.000) == pytest.approx(0.040)
    assert ServoFollower._tick_dt(11.000, 10.000) == C.SERVO_SUBSTEP_MAX_DT_S


def test_small_setpoint_changes_are_not_sent_until_they_are_meaningful():
    last_sent = _pose(0.0)
    assert not ServoFollower._should_write(_pose(C.SERVO_COMMAND_MIN_DELTA - 0.001), last_sent)
    assert ServoFollower._should_write(_pose(C.SERVO_COMMAND_MIN_DELTA), last_sent)
    assert ServoFollower._should_write(_pose(0.001), last_sent, force=True)


def test_worker_sends_the_final_goal_even_when_it_is_below_write_threshold():
    follower = ServoFollower()
    service = _FakeAnimationService()
    running = threading.Event()
    final_goal = C.SERVO_COMMAND_MIN_DELTA / 2
    follower.set_profile(smooth_time=0.001, max_speed_dps=55.0)
    follower.set_goal(_pose(final_goal))
    running.set()

    follower.start(service, running)
    deadline = time.monotonic() + 0.5
    while not service.robot.actions and time.monotonic() < deadline:
        time.sleep(0.01)
    running.clear()
    follower.join(timeout=0.5)

    assert service.robot.actions == [_pose(final_goal)]


def test_worker_coalesces_small_steps_until_they_accumulate(monkeypatch):
    follower = ServoFollower()
    service = _FakeAnimationService()
    running = threading.Event()
    follower.set_goal(_pose(C.SERVO_COMMAND_MIN_DELTA * 4))
    running.set()

    ticks = iter([0.0] + [0.03 * i for i in range(1, 20)])
    monkeypatch.setattr(servo_follow.time, "perf_counter", lambda: next(ticks))
    monkeypatch.setattr(servo_follow.time, "sleep", lambda _: None)

    def send_and_stop(action: dict[str, float]) -> None:
        service.robot.actions.append(dict(action))
        running.clear()

    service.robot.send_action = send_and_stop
    follower._worker(service, running)

    assert len(service.robot.actions) == 1
    first = service.robot.actions[0]["base_yaw.pos"]
    assert C.SERVO_COMMAND_MIN_DELTA <= first < C.SERVO_COMMAND_MIN_DELTA * 4


def _tilt(before: dict, after: dict) -> float:
    """Total camera tilt the joint deltas add up to, positive = down."""
    return sum(
        (after[j] - before[j]) / servo_follow.PITCH_AXIS_SIGN[j]
        for j in servo_follow.PITCH_AXIS_SIGN
    )


def _rest() -> dict[str, float]:
    """A pose like the one idle actually leaves the arm in."""
    return {"base_pitch.pos": 27.0, "elbow_pitch.pos": 24.0, "wrist_pitch.pos": -32.0}


def test_looking_up_does_not_lean_on_the_wrist():
    """Looking up does not use wrist_pitch, which stalls near -34.8."""
    before = _rest()
    after = servo_follow.distribute_pitch(before, -12.0)

    assert abs(after["wrist_pitch.pos"] - before["wrist_pitch.pos"]) <= 2.0
    assert after["elbow_pitch.pos"] > before["elbow_pitch.pos"], "elbow lifts the camera"
    assert _tilt(before, after) == pytest.approx(-12.0, abs=0.01)


def test_looking_down_does_use_the_wrist():
    """Downward is where the wrist has ~65 deg, and it should not go to waste."""
    before = _rest()
    after = servo_follow.distribute_pitch(before, 40.0)

    assert after["wrist_pitch.pos"] > before["wrist_pitch.pos"], (
        "a correction the elbow cannot absorb must reach the wrist"
    )
    assert _tilt(before, after) == pytest.approx(40.0, abs=0.01)


def test_a_joint_at_its_stop_is_never_commanded_further_into_it():
    """The failure this whole allocator exists to make impossible."""
    before = dict(_rest(), **{"wrist_pitch.pos": C.PITCH_TRAVEL_MIN["wrist_pitch.pos"]})
    after = servo_follow.distribute_pitch(before, -15.0)

    assert after["wrist_pitch.pos"] >= before["wrist_pitch.pos"] - 1e-6


def test_a_joint_parked_beyond_its_travel_is_not_dragged_further_out():
    """Idle can leave a joint outside the measured range; do not make it worse."""
    parked = C.PITCH_TRAVEL_MIN["wrist_pitch.pos"] - 5.0
    before = dict(_rest(), **{"wrist_pitch.pos": parked})
    after = servo_follow.distribute_pitch(before, -15.0)

    assert after["wrist_pitch.pos"] >= parked - 1e-6


def test_the_full_correction_is_delivered_when_there_is_room_for_it():
    before = _rest()
    for want in (-10.0, -3.0, 5.0, 18.0):
        after = servo_follow.distribute_pitch(before, want)
        assert _tilt(before, after) == pytest.approx(want, abs=0.01), want


def test_a_correction_with_nowhere_to_go_moves_nothing():
    """Every joint pinned at its upward stop: the caller must see 'no travel'."""
    before = {
        "base_pitch.pos":  C.PITCH_TRAVEL_MIN["base_pitch.pos"],
        "elbow_pitch.pos": C.PITCH_TRAVEL_MAX["elbow_pitch.pos"],
        "wrist_pitch.pos": C.PITCH_TRAVEL_MIN["wrist_pitch.pos"],
    }
    after = servo_follow.distribute_pitch(before, -15.0)
    assert after == pytest.approx(before)


def test_zero_asks_for_nothing():
    before = _rest()
    assert servo_follow.distribute_pitch(before, 0.0) == pytest.approx(before)


class _OwnableService(_FakeAnimationService, BodyOwnership):
    """Animation service stand-in with the real ownership semantics."""

    def __init__(self):
        super().__init__()
        self.seen_while_writing = []


def _run_worker_briefly(follower, service, running, seconds=0.3):
    """Run the worker in a thread and stop it, with a bounded wait."""
    thread = threading.Thread(target=follower._worker, args=(service, running))
    thread.start()
    time.sleep(seconds)
    running.clear()
    thread.join(timeout=3.0)
    assert not thread.is_alive(), "the worker did not stop"


def test_the_follow_worker_owns_the_body_for_as_long_as_it_writes():
    """The follow worker owns the body while it writes."""
    service = _OwnableService()
    running = threading.Event()
    running.set()
    follower = ServoFollower()
    follower.read_initial_positions(service)

    service._tracking_active = False

    owned = []
    real_send = service.robot.send_action

    def watching(action):
        owned.append(service._tracking_active)
        real_send(action)

    service.robot.send_action = watching
    follower.set_goal(dict(_pose(40.0)))
    _run_worker_briefly(follower, service, running)

    assert owned, "precondition: the worker wrote at least one frame"
    assert all(owned), "the body must read as owned while the follower writes"
    assert not service._tracking_active, "and free again once the worker stops"


def test_ownership_is_released_even_if_the_follow_loop_raises(monkeypatch):
    """Ownership is released even if the follow loop raises."""
    service = _OwnableService()
    running = threading.Event()
    running.set()
    follower = ServoFollower()

    def boom(*args, **kwargs):
        raise RuntimeError("bus")

    monkeypatch.setattr(follower, "_follow", boom)
    with pytest.raises(RuntimeError):
        follower._worker(service, running)

    assert not service._tracking_active, "a crashed worker must not wedge the lock"


def _pan_rest() -> dict[str, float]:
    return {"base_yaw.pos": 0.0, "wrist_roll.pos": 0.0}


def test_a_face_on_the_right_turns_the_camera_right():
    """dx > 0 is corrected by increasing both base_yaw and wrist_roll."""
    before = _pan_rest()
    after = servo_follow.distribute_yaw(before, +10.0)

    assert after["base_yaw.pos"] > 0.0
    assert after["wrist_roll.pos"] > 0.0


def test_a_face_on_the_left_turns_the_camera_left():
    before = _pan_rest()
    after = servo_follow.distribute_yaw(before, -10.0)

    assert after["base_yaw.pos"] < 0.0
    assert after["wrist_roll.pos"] < 0.0


def test_the_base_leads_the_pan():
    """base_yaw carries the correction, not the wrist."""
    before = _pan_rest()
    after = servo_follow.distribute_yaw(before, +10.0)

    assert after["base_yaw.pos"] > after["wrist_roll.pos"]


def test_the_full_pan_is_delivered_when_there_is_room():
    before = _pan_rest()
    for want in (-12.0, -4.0, 3.0, 9.0):
        after = servo_follow.distribute_yaw(before, want)
        total = sum(after[j] - before[j] for j in after)
        assert total == pytest.approx(want, abs=0.01), want


def test_a_saturated_yaw_hands_the_rest_to_the_wrist():
    """base_yaw pinned at its travel limit: the pan must still happen."""
    before = {"base_yaw.pos": C.YAW_TRAVEL_MAX["base_yaw.pos"], "wrist_roll.pos": 0.0}
    after = servo_follow.distribute_yaw(before, +10.0)

    assert after["base_yaw.pos"] == pytest.approx(before["base_yaw.pos"])
    assert after["wrist_roll.pos"] == pytest.approx(10.0, abs=0.01)


def test_a_pan_with_nowhere_to_go_moves_nothing():
    before = {
        "base_yaw.pos":   C.YAW_TRAVEL_MAX["base_yaw.pos"],
        "wrist_roll.pos": C.YAW_TRAVEL_MAX["wrist_roll.pos"],
    }
    assert servo_follow.distribute_yaw(before, +10.0) == pytest.approx(before)


def test_pitch_and_pan_do_not_touch_each_other_s_joints():
    """A tilt must never pan, and a pan must never tilt."""
    pitch = servo_follow.distribute_pitch(
        {"base_pitch.pos": 20.0, "elbow_pitch.pos": 10.0, "wrist_pitch.pos": -20.0}, -8.0)
    assert "base_yaw.pos" not in pitch and "wrist_roll.pos" not in pitch

    pan = servo_follow.distribute_yaw(_pan_rest(), 8.0)
    assert not ({"base_pitch.pos", "elbow_pitch.pos", "wrist_pitch.pos"} & set(pan))
