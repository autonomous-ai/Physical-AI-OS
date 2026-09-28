"""The range demo: a narrated tour of what the body can actually do."""

from unittest import mock

import pytest

import hal.app_state as state
from hal.drivers.motors import range_demo
from hal.drivers.tracking import constants as C
from test.body_ownership import BodyOwnership


class _FakeSvc(BodyOwnership):
    # Head low at rest: the posture with the least room above. PITCH_LOOK_DEG must still fit.
    REST = {
        "base_yaw.pos": 3.0, "base_pitch.pos": 29.8, "elbow_pitch.pos": 27.1,
        "wrist_pitch.pos": -61.7, "wrist_roll.pos": 8.2,
    }

    UNWRITTEN_SPEED_EQUIVALENT = 0

    def __init__(self):
        self._pos = dict(self.REST)
        self.holds = []
        self.speeds = []

    def get_positions(self):
        return dict(self._pos)

    def set_joint_speed(self, motor_name, speed):
        self.speeds.append((motor_name, speed))
        return True

    def move_and_hold(self, target, duration=None):
        self.holds.append(dict(target))
        self._pos.update({j: float(v) for j, v in target.items()})
        return dict(self._pos)


@pytest.fixture(autouse=True)
def _reset():
    range_demo._abort_evt.clear()
    range_demo._running.clear()
    yield
    range_demo._abort_evt.clear()
    range_demo._running.clear()


def _run(svc, say=None):
    """Run the demo with the body owned and the waits stubbed out."""
    said = [] if say is None else say
    with (
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "animation_service", svc),
        mock.patch("hal.drivers.tracking.aim._say", said.append),
        mock.patch.object(range_demo, "_wait_until_still"),
        mock.patch.object(range_demo.time, "sleep"),
    ):
        return range_demo.run(svc), said


def test_the_demo_reaches_the_actual_yaw_limits():
    """Waypoints come from joint limits, not a recording."""
    yaws = [p["base_yaw.pos"] for p, _pool in range_demo.waypoints(_FakeSvc.REST)
            if "base_yaw.pos" in p]
    assert min(yaws) == pytest.approx(C.YAW_MIN, abs=0.01)
    assert max(yaws) == pytest.approx(C.YAW_MAX, abs=0.01)
    assert max(yaws) - min(yaws) > 250, (
        f"the demo covers {max(yaws) - min(yaws):.1f}° of "
        f"{C.YAW_MAX - C.YAW_MIN:.0f}°")


def test_the_pitch_legs_stay_inside_travel_the_device_is_known_to_have():
    """The demo stays within the measured wrist_pitch range, not the declared limit."""
    seed = _FakeSvc.REST["wrist_pitch.pos"]
    pitches = [p["wrist_pitch.pos"] for p, _pool in range_demo.waypoints(_FakeSvc.REST)
               if "wrist_pitch.pos" in p]
    assert pitches, "the demo never tilts"
    for wp in pitches:
        assert abs(wp - seed) <= range_demo.PITCH_LOOK_DEG + 0.01, (
            f"pitch leg {wp:+.1f} is {abs(wp - seed):.1f}° from the seed — "
            f"beyond the {range_demo.PITCH_LOOK_DEG}° the sweep has proven")
        assert C.WRIST_PITCH_MIN <= wp <= C.WRIST_PITCH_MAX


def test_every_joint_is_narrated_at_least_once():
    """Every joint the demo moves is announced when it starts."""
    wps = range_demo.waypoints(_FakeSvc.REST)
    announced = set()
    for pose, pool in wps:
        joint = next(iter(pose))
        if pool:
            announced.add(joint)
    moved = {next(iter(pose)) for pose, _ in wps}
    assert moved == announced, f"joints moved without a word: {moved - announced}"


def test_every_joint_gets_a_turn_and_returns_to_centre_before_the_next():
    """Each joint performs alone and returns home before the next."""
    wps = range_demo.waypoints(_FakeSvc.REST)
    joints_in_order = []
    for pose, _ in wps:
        j = next(iter(pose))
        if not joints_in_order or joints_in_order[-1] != j:
            joints_in_order.append(j)
    assert joints_in_order == [
        "base_yaw.pos", "wrist_roll.pos", "wrist_pitch.pos", "elbow_pitch.pos", "base_pitch.pos",
    ], joints_in_order
    for joint in joints_in_order:
        last = [pose[joint] for pose, _ in wps if joint in pose][-1]
        assert last == pytest.approx(_FakeSvc.REST[joint]), f"{joint} does not return to centre"


def test_the_small_joints_stay_inside_their_travel():
    """The demo never commands past the travel table."""
    # PITCH_TRAVEL does not cover wrist_pitch; that joint is checked in its own test.
    wps = range_demo.waypoints(_FakeSvc.REST)
    for pose, _ in wps:
        for joint, v in pose.items():
            if joint in ("elbow_pitch.pos", "base_pitch.pos"):
                assert C.PITCH_TRAVEL_MIN[joint] <= v <= C.PITCH_TRAVEL_MAX[joint], (
                    f"{joint}={v:+.1f} outside {C.PITCH_TRAVEL_MIN[joint]}..{C.PITCH_TRAVEL_MAX[joint]}")
            if joint == "wrist_roll.pos":
                assert C.WRIST_ROLL_MIN <= v <= C.WRIST_ROLL_MAX


def test_the_demo_speaks_every_narrated_leg_in_order_and_nothing_for_silent_ones():
    svc = _FakeSvc()
    res, said = _run(svc)
    assert res["completed"] is True
    assert said[0] == "demo_intro"
    assert said[-1] == "demo_done"
    assert said[1:-1] == [pool for _p, pool in range_demo.waypoints(_FakeSvc.REST) if pool]
    assert "" not in said, "an empty pool reached the filler endpoint"


def test_each_leg_is_announced_then_performed_and_never_overlaps_the_next():
    """Each phrase is spoken before its leg moves."""
    svc = _FakeSvc()
    order = []
    with (
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "animation_service", svc),
        mock.patch("hal.drivers.tracking.aim._say", lambda p: order.append(("say", p))),
        mock.patch.object(range_demo, "_wait_until_still",
                          lambda *_a, **_k: order.append(("settle", None))),
        mock.patch.object(range_demo.time, "sleep"),
    ):
        range_demo.run(svc)

    legs = [pool for _p, pool in range_demo.waypoints(_FakeSvc.REST) if pool]
    at = {v: i for i, (k, v) in enumerate(order) if k == "say"}
    for pool in legs:
        assert pool in at, f"{pool} was never spoken"
        assert order[at[pool] + 1][0] == "settle", (
            f"{pool} was spoken but nothing moved after it")
    for previous, nxt in zip(legs, legs[1:]):
        assert any(order[j][0] == "settle" for j in range(at[previous], at[nxt])), (
            f"{nxt} was announced before {previous}'s movement had finished")


def test_the_base_is_sped_up_for_the_demo_and_put_back():
    """The base speed is raised for the demo and restored after."""
    svc = _FakeSvc()
    _run(svc)
    assert svc.speeds, "the demo never touched the base speed"
    assert svc.speeds[0] == ("base_yaw", range_demo.DEMO_YAW_SPEED)
    assert svc.speeds[-1] == ("base_yaw", _FakeSvc.UNWRITTEN_SPEED_EQUIVALENT)


def test_an_abort_stops_the_body_and_the_narration_together():
    """A single click stops both the motion and the narration."""
    svc = _FakeSvc()
    said = []

    def _say(pool):
        said.append(pool)
        if len(said) == 2:
            range_demo.request_abort()

    with (
        mock.patch.object(state, "safety_policy", None),
        mock.patch.object(state, "animation_service", svc),
        mock.patch("hal.drivers.tracking.aim._say", _say),
        mock.patch.object(range_demo, "_wait_until_still"),
        mock.patch.object(range_demo.time, "sleep"),
    ):
        res = range_demo.run(svc)

    assert res["completed"] is False
    assert res["reason"] == "aborted"
    assert len(said) <= 3, f"kept narrating after the abort: {said}"
    assert "demo_done" not in said
    assert svc.holds[-1] == _FakeSvc.REST, "an aborted demo must go home"


def test_the_demo_ends_where_it_started():
    svc = _FakeSvc()
    _run(svc)
    assert svc.holds[-1] == _FakeSvc.REST


def test_a_sleeping_device_does_not_perform():
    svc = _FakeSvc()
    with mock.patch.object(state, "_sleeping", True, create=True):
        res = range_demo.start(svc)
    assert res["started"] is False
    assert res["reason"] == "sleeping"
    assert svc.holds == []


def test_a_second_demo_does_not_start_on_top_of_a_running_one():
    """Two performances sharing one body is two half-performances."""
    svc = _FakeSvc()
    range_demo._running.set()
    with mock.patch.object(state, "_sleeping", False, create=True):
        res = range_demo.start(svc)
    assert res["started"] is False
    assert res["reason"] == "already running"
    assert svc.holds == []


def test_start_returns_immediately_rather_than_performing():
    """The demo route returns immediately and runs in the background."""
    svc = _FakeSvc()
    with (
        mock.patch.object(state, "_sleeping", False, create=True),
        mock.patch.object(range_demo.threading, "Thread") as thread,
    ):
        res = range_demo.start(svc)
    assert res["started"] is True
    assert res["waypoints"] == len(range_demo.waypoints(_FakeSvc.REST))
    thread.assert_called_once()
    assert thread.call_args.kwargs["daemon"] is True
    assert svc.holds == [], "start() performed the demo on the caller's thread"


def _demo_client():
    """A TestClient over the servo router — no hardware, no app startup."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hal.routes.servo import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_the_route_starts_the_demo_and_returns_at_once():
    with (
        mock.patch("hal.routes.servo._sleep_servo_locked", return_value=False),
        mock.patch("hal.routes.servo._svc_connected") as svc,
        mock.patch("hal.drivers.motors.range_demo.start",
                   return_value={"started": True, "waypoints": 4,
                                 "reason": "started"}) as start,
    ):
        svc.return_value.is_suppressed = False
        body = _demo_client().post("/servo/demo").json()

    start.assert_called_once()
    assert body == {"status": "ok", "started": True, "waypoints": 4,
                    "reason": "started"}


def test_the_route_refuses_while_the_device_sleeps():
    """The demo is refused while the device sleeps."""
    with (
        mock.patch("hal.routes.servo._sleep_servo_locked", return_value=True),
        mock.patch("hal.drivers.motors.range_demo.start") as start,
    ):
        body = _demo_client().post("/servo/demo").json()

    start.assert_not_called()
    assert body["started"] is False
    assert body["reason"] == "sleeping"


def test_the_single_click_aborts_the_demo_with_the_aim_and_the_sweep():
    """The single click aborts the demo alongside the aim and the sweep."""
    from hal.drivers import button_actions

    with (
        mock.patch("hal.drivers.motors.range_demo.request_abort") as abort_demo,
        mock.patch("hal.drivers.tracking.aim.request_abort"),
        mock.patch("hal.drivers.tracking.search.request_abort"),
        mock.patch.object(state, "tracker_service", None),
    ):
        button_actions._stop_active_tracking("test")

    abort_demo.assert_called_once()
