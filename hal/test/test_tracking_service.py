"""Focused lifecycle tests for object tracking."""

import threading

from hal.drivers.tracking import constants as C
from hal.drivers.tracking import tracker_service
from hal.drivers.tracking.tracker_service import TrackerService, TrackingState


class _FakeBus:
    def __init__(self):
        self.writes: list[tuple[str, dict[str, int]]] = []

    def sync_write(self, register: str, values: dict[str, int]) -> None:
        self.writes.append((register, dict(values)))


class _FakeRobot:
    def __init__(self):
        self.bus = _FakeBus()
        self.actions: list[dict[str, float]] = []

    def send_action(self, action: dict[str, float]) -> None:
        self.actions.append(dict(action))


class _FakeAnimationService:
    def __init__(self):
        self.bus_lock = threading.RLock()
        self.robot = _FakeRobot()
        self._hold_mode = False
        self._tracking_active = False
        self._running = threading.Event()
        self._running.set()
        self.dispatched: list[tuple[str, str]] = []
        self.idle_recording = "idle"
        self.positions = {
            "base_yaw.pos": 18.0,
            "base_pitch.pos": 7.0,
            "elbow_pitch.pos": -4.0,
            "wrist_roll.pos": 3.0,
            "wrist_pitch.pos": -12.0,
        }
        self._current_state = {"base_yaw.pos": -30.0}

    def dispatch(self, event: str, recording: str) -> None:
        self.dispatched.append((event, recording))

    def get_positions(self) -> dict[str, float]:
        return dict(self.positions)


class _FakeFollower:
    def __init__(self):
        self.joined = False
        self.goal_cleared = False

    def read_initial_positions(self, _service) -> None:
        pass

    def seed_goal_current(self) -> None:
        pass

    def start(self, _service, _running) -> None:
        pass

    def join(self, timeout: float) -> None:
        self.joined = timeout == 2.0

    def clear_goal(self) -> None:
        self.goal_cleared = True


class _FakePID:
    def reset(self) -> None:
        pass


class _NoFrameCamera:
    last_frame = None


def test_tracking_timeout_stops_even_when_camera_has_no_frames(monkeypatch):
    service = object.__new__(TrackerService)
    state = TrackingState(target_label="object")
    state.running.set()
    service._state = state
    service._follower = _FakeFollower()
    service._yaw_pid = _FakePID()
    service._pitch_pid = _FakePID()

    animation = _FakeAnimationService()
    clock = iter((0.0, C.MAX_TRACK_DURATION_S + 0.1, C.MAX_TRACK_DURATION_S + 0.1))
    monkeypatch.setattr(tracker_service.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(tracker_service.time, "sleep", lambda _seconds: None)

    service._track_loop(_NoFrameCamera(), animation)

    assert not state.running.is_set()
    assert not animation._tracking_active
    assert not animation._hold_mode
    assert service._follower.joined
    assert service._follower.goal_cleared
    assert animation.robot.actions == []
    assert animation._current_state == animation.positions
    assert animation.dispatched == [("play", "idle")]


def test_a_tracking_session_leaves_a_scene_hold_in_place(monkeypatch):
    from hal.drivers.motors import hold

    service = object.__new__(TrackerService)
    state = TrackingState(target_label="object")
    state.running.set()
    service._state = state
    service._follower = _FakeFollower()
    service._yaw_pid = _FakePID()
    service._pitch_pid = _FakePID()

    animation = _FakeAnimationService()
    hold.claim(animation, hold.SCENE)
    clock = iter((0.0, C.MAX_TRACK_DURATION_S + 0.1, C.MAX_TRACK_DURATION_S + 0.1))
    monkeypatch.setattr(tracker_service.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(tracker_service.time, "sleep", lambda _seconds: None)

    service._track_loop(_NoFrameCamera(), animation)

    assert not animation._tracking_active
    assert animation._hold_mode
    assert hold.owners(animation) == {"scene"}
