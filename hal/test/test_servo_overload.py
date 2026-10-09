"""Servo overload cut-off: the guard's timing, and the service's cut / lockout / retry."""

import sys
import types
from unittest import mock

import pytest

from hal.drivers.motors.overload import OverloadGuard, load_magnitude

JOINTS = ("base_yaw", "base_pitch", "elbow_pitch", "wrist_roll", "wrist_pitch")
IDLE = {joint: 120 for joint in JOINTS}


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _guard(threshold=800, hold_s=1.0, retry_s=120.0):
    clock = _Clock()
    return OverloadGuard(threshold, hold_s, retry_s, clock=clock), clock


def _stalled(joint="elbow_pitch", load=950):
    return {**IDLE, joint: load}


# --- load_magnitude -----------------------------------------------------------


def test_load_magnitude_drops_the_direction_bit():
    assert load_magnitude(900) == 900
    assert load_magnitude(0x400 | 900) == 900
    assert load_magnitude(1000) == 1000
    assert load_magnitude(0x400 | 1000) == 1000
    assert load_magnitude(0) == 0


# --- OverloadGuard ------------------------------------------------------------


def test_normal_load_never_trips():
    guard, clock = _guard()
    for _ in range(100):
        assert guard.observe(IDLE) is None
        clock.advance(0.1)
    assert not guard.locked
    assert guard.trips == 0


def test_a_spike_shorter_than_the_hold_does_not_trip():
    guard, clock = _guard()
    assert guard.observe(_stalled()) is None
    clock.advance(0.9)
    assert guard.observe(_stalled()) is None
    clock.advance(0.1)
    assert guard.observe(IDLE) is None
    clock.advance(0.1)
    # The earlier spike must not count towards a later one.
    assert guard.observe(_stalled()) is None
    clock.advance(0.9)
    assert guard.observe(_stalled()) is None
    assert not guard.locked


def test_sustained_high_load_trips_once_the_hold_has_passed():
    guard, clock = _guard()
    assert guard.observe(_stalled()) is None
    clock.advance(0.5)
    assert guard.observe(_stalled()) is None
    clock.advance(0.5)
    assert guard.observe(_stalled()) == ("elbow_pitch", 950)
    assert guard.locked
    assert guard.trips == 1
    assert guard.last_trip == ("elbow_pitch", 950)
    assert guard.retry_in_s() == 120.0


def test_load_exactly_at_the_threshold_counts():
    guard, clock = _guard()
    guard.observe(_stalled(load=800))
    clock.advance(1.0)
    assert guard.observe(_stalled(load=800)) == ("elbow_pitch", 800)


def test_the_most_loaded_stalled_joint_is_reported():
    guard, clock = _guard()
    both = {**IDLE, "base_pitch": 850, "wrist_pitch": 990}
    guard.observe(both)
    clock.advance(1.0)
    assert guard.observe(both) == ("wrist_pitch", 990)


def test_a_failed_read_restarts_the_timing():
    guard, clock = _guard()
    guard.observe(_stalled())
    clock.advance(5.0)
    assert guard.observe(None) is None
    # Two high samples either side of a gap are not one sustained overload.
    assert guard.observe(_stalled()) is None
    clock.advance(0.5)
    assert guard.observe({}) is None
    clock.advance(0.5)
    assert guard.observe(_stalled()) is None
    assert not guard.locked


def test_nothing_is_observed_during_the_lockout_and_retry_is_due_once():
    guard, clock = _guard()
    guard.observe(_stalled())
    clock.advance(1.0)
    assert guard.observe(_stalled()) is not None
    clock.advance(60.0)
    assert guard.observe(_stalled()) is None
    assert not guard.retry_due()
    assert guard.retry_in_s() == 60.0
    clock.advance(60.0)
    assert guard.retry_due()
    assert not guard.locked
    assert not guard.retry_due()
    assert guard.retry_in_s() == 0.0


def test_it_trips_again_after_the_retry_if_still_stalled():
    guard, clock = _guard()
    guard.observe(_stalled())
    clock.advance(1.0)
    guard.observe(_stalled())
    clock.advance(120.0)
    assert guard.retry_due()
    # A fresh hold is needed: the pre-lockout timing does not carry over.
    assert guard.observe(_stalled()) is None
    clock.advance(1.0)
    assert guard.observe(_stalled()) == ("elbow_pitch", 950)
    assert guard.trips == 2


def test_threshold_zero_disables_the_guard():
    guard, clock = _guard(threshold=0)
    assert not guard.enabled
    for _ in range(30):
        assert guard.observe(_stalled(load=1000)) is None
        clock.advance(0.1)
    assert not guard.locked


def test_latest_and_peak_load_are_kept_for_tuning():
    guard, clock = _guard()
    guard.observe({**IDLE, "base_yaw": 400})
    clock.advance(0.1)
    guard.observe({**IDLE, "base_yaw": 250})
    assert guard.load["base_yaw"] == 250
    assert guard.peak["base_yaw"] == 400
    assert guard.peak["elbow_pitch"] == 120


# --- AnimationService integration (fake bus, no hardware) -----------------------


class _FakeBus:
    def __init__(self):
        self.motors = {joint: types.SimpleNamespace(id=i + 1) for i, joint in enumerate(JOINTS)}
        self.loads = dict(IDLE)
        self.fail_read = False
        self.writes = []
        self.raw_writes = []
        self.port_handler = object()
        self.packet_handler = self

    def sync_read(self, name, normalize=True):
        assert (name, normalize) == ("Present_Load", False)
        if self.fail_read:
            raise ConnectionError("no status packet")
        return dict(self.loads)

    def write(self, name, motor, value):
        self.writes.append((name, motor, value))

    # scservo_sdk packet-handler surface used by the raw paths.
    def ping(self, ph, sid):
        return 0, 0, 0

    def read2ByteTxRx(self, ph, sid, reg):
        return 2048, 0, 0

    def write1ByteTxRx(self, ph, sid, reg, value):
        self.raw_writes.append((sid, reg, value))

    def write2ByteTxRx(self, ph, sid, reg, value):
        self.raw_writes.append((sid, reg, value))


class _FakeRobot:
    is_connected = True

    def __init__(self):
        self.bus = _FakeBus()
        self.goal_writes_blocked = False
        self.sent = []

    def send_action(self, action):
        # Mirrors LeLampFollower.send_action's overload gate.
        if self.goal_writes_blocked:
            return {}
        self.sent.append(dict(action))
        return action


def _animation_service_cls():
    """AnimationService, with hal.follower stubbed where lerobot is not installed."""
    try:
        import hal.follower  # noqa: F401
    except Exception:
        stub = types.ModuleType("hal.follower")
        stub.LeLampFollowerConfig = lambda **kwargs: types.SimpleNamespace(**kwargs)
        stub.LeLampFollower = object
        with mock.patch.dict(sys.modules, {"hal.follower": stub}):
            from hal.drivers.motors.animation_service import AnimationService
        return AnimationService
    from hal.drivers.motors.animation_service import AnimationService
    return AnimationService


@pytest.fixture
def rig():
    """A service wired to a fake bus, driven tick by tick (the monitor thread never starts)."""
    guard, clock = _guard()
    fired = []
    svc = _animation_service_cls()(
        port="/dev/null", lamp_id="test",
        overload_guard=guard, on_overload=lambda joint, load: fired.append((joint, load)),
    )
    svc.robot = _FakeRobot()
    svc._running.set()
    svc.resume = mock.Mock()
    return types.SimpleNamespace(svc=svc, guard=guard, clock=clock, fired=fired, bus=svc.robot.bus)


def _stall(rig, joint="elbow_pitch", load=950):
    """Hold one joint over the threshold until the cut-off fires."""
    rig.bus.loads = _stalled(joint, load)
    rig.svc._overload_tick()
    rig.clock.advance(1.0)
    rig.svc._overload_tick()


def test_normal_load_leaves_the_servos_alone(rig):
    for _ in range(50):
        rig.svc._overload_tick()
        rig.clock.advance(0.1)
    assert rig.bus.writes == []
    assert not rig.svc.overload_active
    assert rig.fired == []


def test_a_stall_cuts_torque_on_every_servo_and_reports_it(rig):
    _stall(rig)
    assert rig.bus.writes == [("Torque_Enable", joint, 0) for joint in JOINTS]
    assert rig.svc.robot.goal_writes_blocked is True
    assert rig.svc._halt.is_set()
    assert rig.svc.overload_active
    assert rig.fired == [("elbow_pitch", 950)]


def test_the_direction_bit_does_not_hide_a_stall(rig):
    _stall(rig, load=0x400 | 950)
    assert rig.fired == [("elbow_pitch", 950)]


def test_a_brief_spike_does_not_cut_torque(rig):
    rig.bus.loads = _stalled()
    rig.svc._overload_tick()
    rig.clock.advance(0.5)
    rig.bus.loads = dict(IDLE)
    rig.svc._overload_tick()
    rig.clock.advance(1.0)
    rig.svc._overload_tick()
    assert rig.bus.writes == []
    assert not rig.svc.overload_active


def test_a_failed_load_read_never_cuts_torque(rig):
    rig.bus.loads = _stalled()
    rig.bus.fail_read = True
    for _ in range(30):
        rig.svc._overload_tick()
        rig.clock.advance(0.1)
    assert rig.bus.writes == []
    assert not rig.svc.overload_active


def test_a_failing_handler_does_not_undo_the_cut(rig):
    rig.svc._on_overload = mock.Mock(side_effect=RuntimeError("tts down"))
    _stall(rig)
    assert rig.svc.overload_active
    assert rig.svc.robot.goal_writes_blocked is True


def test_commanded_moves_are_refused_during_the_lockout(rig):
    _stall(rig)
    target = {"base_yaw.pos": 10.0}
    for call in (
        lambda: rig.svc.move_to(target, duration=0.1),
        lambda: rig.svc.move_and_hold(target, duration=0.1),
        lambda: rig.svc.move_and_hold(target, duration=0),
        lambda: rig.svc.send_positions(target),
    ):
        with pytest.raises(RuntimeError, match="overload cut-off active"):
            call()
    assert rig.svc.robot.sent == []


def test_no_goal_or_torque_on_write_reaches_the_bus_during_the_lockout(rig):
    _stall(rig)
    rig.svc.halt()                                  # would pin the current pose
    rig.svc.robot.send_action({"base_yaw.pos": 5.0})  # tracker / playback path
    rig.svc.move_to_raw({"base_yaw": 2048}, duration=0.1)
    with mock.patch.dict(sys.modules, {"scservo_sdk": types.SimpleNamespace(COMM_SUCCESS=0)}):
        rig.svc._configure_servos_raw(energize=True)
    assert rig.svc.robot.sent == []
    goal_or_torque_on = [
        w for w in rig.bus.raw_writes if w[1] == 42 or (w[1] == 40 and w[2] == 1)
    ]
    assert goal_or_torque_on == []


def _trip_now(rig):
    """Cut immediately, the way observe() + _overload_cut() do, from inside a bus call."""
    rig.guard._locked_until = rig.clock() + rig.guard.retry_s
    rig.svc._overload_cut("elbow_pitch", 950)


def test_a_cut_mid_raw_move_stops_its_remaining_goal_writes(rig):
    """Startup / zero / park moves must not keep re-engaging torque after the cut."""
    rig.svc.fps = 100
    goals_before_cut = []
    real = rig.bus.write2ByteTxRx

    def write2(ph, sid, reg, value):
        real(ph, sid, reg, value)
        # Trips on the last write of frame 2's batch: a real cut needs the bus lock, so it
        # can only land between batches.
        if reg == 42 and len(goals_before_cut) == 0 and len(rig.bus.raw_writes) >= 4:
            goals_before_cut.append(len([w for w in rig.bus.raw_writes if w[1] == 42]))
            _trip_now(rig)

    rig.bus.write2ByteTxRx = write2
    rig.svc.move_to_raw({"base_yaw": 2048, "base_pitch": 2048}, duration=0.05)
    goals_total = len([w for w in rig.bus.raw_writes if w[1] == 42])
    assert goals_before_cut and goals_total == goals_before_cut[0]
    assert rig.svc.overload_active


def test_a_cut_landing_before_configure_takes_the_lock_still_wins(rig):
    """The torque-on decision is made under the bus lock, not before it."""
    real_lock = rig.svc.bus_lock
    tripped = []

    class Lock:
        def __enter__(self):
            if not tripped:
                tripped.append(True)
                _trip_now(rig)
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)

    rig.svc.bus_lock = Lock()
    with mock.patch.dict(sys.modules, {"scservo_sdk": types.SimpleNamespace(COMM_SUCCESS=0)}):
        rig.svc._configure_servos_raw(energize=True)
    assert [w for w in rig.bus.raw_writes if w[1] == 40 and w[2] == 1] == []


def test_a_failed_torque_off_is_retried_until_confirmed(rig):
    failing = {"wrist_pitch"}
    real = rig.bus.write

    def flaky(name, motor, value):
        if motor in failing:
            raise ConnectionError("no status packet")
        real(name, motor, value)

    rig.bus.write = flaky
    _stall(rig)
    status = rig.svc.overload_status()
    assert status["active"] and status["cut_complete"] is False
    assert status["pending_off"] == ["wrist_pitch"]
    assert rig.svc.robot.goal_writes_blocked is True
    # Still failing: retried every tick, still pending.
    rig.clock.advance(0.1)
    rig.svc._overload_tick()
    assert rig.svc.overload_status()["pending_off"] == ["wrist_pitch"]
    # Bus recovers: the next tick confirms it.
    failing.clear()
    rig.clock.advance(0.1)
    rig.svc._overload_tick()
    status = rig.svc.overload_status()
    assert status["cut_complete"] is True and status["pending_off"] == []
    assert ("Torque_Enable", "wrist_pitch", 0) in rig.bus.writes
    assert rig.svc.overload_active


def test_a_torque_off_that_never_confirms_is_dropped_at_recovery(rig):
    rig.bus.write = mock.Mock(side_effect=ConnectionError("bus down"))
    _stall(rig)
    assert rig.svc.overload_status()["pending_off"] == sorted(JOINTS)
    for _ in range(5):
        rig.clock.advance(0.1)
        rig.svc._overload_tick()
    assert rig.bus.write.call_count == len(JOINTS) * 6
    rig.clock.advance(120.0)
    rig.svc._overload_tick()
    assert not rig.svc.overload_active
    assert rig.svc.overload_status()["pending_off"] == []
    rig.svc.resume.assert_called_once_with()


def test_configure_still_energizes_outside_a_lockout(rig):
    with mock.patch.dict(sys.modules, {"scservo_sdk": types.SimpleNamespace(COMM_SUCCESS=0)}):
        rig.svc._configure_servos_raw(energize=True)
    assert [(sid, 40, 1) for sid in range(1, 6)] == [
        w for w in rig.bus.raw_writes if w[1] == 40 and w[2] == 1
    ]


def test_the_lockout_ends_after_the_retry_delay_with_a_resume(rig):
    _stall(rig)
    rig.clock.advance(119.0)
    rig.svc._overload_tick()
    assert rig.svc.overload_active
    rig.svc.resume.assert_not_called()
    rig.clock.advance(1.0)
    rig.svc._overload_tick()
    assert not rig.svc.overload_active
    assert rig.svc.robot.goal_writes_blocked is False
    rig.svc.resume.assert_called_once_with()


def test_a_body_parked_during_the_lockout_is_not_woken_by_the_retry(rig):
    _stall(rig)
    rig.svc._running.clear()        # release() (sleep) or zero_pose() stopped the loop
    rig.clock.advance(120.0)
    rig.svc._overload_tick()
    assert not rig.svc.overload_active
    assert rig.svc.robot.goal_writes_blocked is False
    rig.svc.resume.assert_not_called()


def test_still_blocked_after_the_retry_cuts_again(rig):
    _stall(rig)
    rig.clock.advance(120.0)
    rig.svc._overload_tick()
    rig.bus.writes.clear()
    _stall(rig)
    assert rig.bus.writes == [("Torque_Enable", joint, 0) for joint in JOINTS]
    assert rig.fired == [("elbow_pitch", 950), ("elbow_pitch", 950)]
    assert rig.guard.trips == 2


def test_status_reports_the_lockout_for_health(rig):
    status = rig.svc.overload_status()
    assert status["active"] is False and status["trips"] == 0 and status["last_trip"] is None
    _stall(rig)
    rig.clock.advance(20.0)
    status = rig.svc.overload_status()
    assert status["active"] is True
    assert status["retry_in_s"] == 100.0
    assert status["last_trip"] == {"joint": "elbow_pitch", "load": 950}
    assert (status["threshold"], status["hold_s"], status["retry_s"]) == (800, 1.0, 120.0)
    assert status["peak"]["elbow_pitch"] == 950


def test_a_service_without_a_guard_has_no_cut_off():
    svc = _animation_service_cls()(port="/dev/null", lamp_id="test")
    assert svc.overload_status() is None
    assert not svc.overload_active
    svc._refuse_if_overloaded()


def test_the_follower_drops_goals_while_blocked():
    """The real gate, where lerobot is installed."""
    pytest.importorskip("lerobot")
    from hal.follower.hal_follower import LeLampFollower

    robot = types.SimpleNamespace(
        is_connected=True, goal_writes_blocked=True, bus=mock.Mock(),
        config=types.SimpleNamespace(max_relative_target=None), last_write_monotonic=0.0,
    )
    assert LeLampFollower.send_action(robot, {"base_yaw.pos": 5.0}) == {}
    robot.bus.sync_write.assert_not_called()
    robot.goal_writes_blocked = False
    LeLampFollower.send_action(robot, {"base_yaw.pos": 5.0})
    robot.bus.sync_write.assert_called_once_with("Goal_Position", {"base_yaw": 5.0})
