"""Overlapping camera consumers must not release each other's motion freeze."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from hal.drivers.camera.video_capture_device import capture_still
from hal.drivers.motors.freeze_lease import freeze_lease
from hal.drivers.sensing.sensing_service import SensingService
from hal.drivers.tracking import tracker_service


class Motor:
    last_servo_write = 0.0

    def __init__(self):
        self.frozen = False
        self.events = []

    def freeze(self):
        self.frozen = True
        self.events.append("freeze")

    def unfreeze(self):
        self.frozen = False
        self.events.append("unfreeze")


class Capture:
    def __init__(self, motor, block=False):
        self.motor = motor
        self.consumers = 0
        self.entered = threading.Event()
        self.finish = threading.Event()
        if not block:
            self.finish.set()

    @property
    def last_frame_ts(self):
        return time.monotonic()

    @property
    def last_frame(self):
        self.entered.set()
        assert self.finish.wait(3), "capture was serialized or test failed to release it"
        assert self.motor.frozen
        return np.zeros((2, 2, 3), dtype=np.uint8)

    def acquire_consumer(self):
        self.consumers += 1

    def release_consumer(self):
        self.consumers -= 1


def test_two_snapshots_overlap_without_releasing_first_freeze():
    motor = Motor()
    first, second = Capture(motor, block=True), Capture(motor)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(capture_still, first, motor)
        try:
            assert first.entered.wait(3)
            assert pool.submit(capture_still, second, motor).result(timeout=3) is not None
            assert motor.frozen
            assert motor.events == ["freeze"]
            assert second.consumers == 0
        finally:
            first.finish.set()
        assert pending.result(timeout=3) is not None
    assert first.consumers == 0
    assert motor.events == ["freeze", "unfreeze"]


@pytest.mark.parametrize("method", ["acquire_consumer", "release_consumer"])
def test_snapshot_consumer_failure_releases_freeze(method):
    motor = Motor()
    cap = Capture(motor)

    def fail():
        raise RuntimeError("consumer failed")

    setattr(cap, method, fail)
    with pytest.raises(RuntimeError, match="consumer failed"):
        capture_still(cap, motor)
    assert not motor.frozen
    assert motor.events == ["freeze", "unfreeze"]


def test_sensing_exception_keeps_snapshot_freeze():
    motor = Motor()
    service = object.__new__(SensingService)
    service._animation_service = motor
    service.FREEZE_SETTLE_S = 0

    def fail_capture():
        assert motor.frozen
        raise RuntimeError("camera failed")

    service._camera = SimpleNamespace(capture=fail_capture)
    with freeze_lease(motor):
        with pytest.raises(RuntimeError, match="camera failed"):
            service._capture_stable_frame()
        assert motor.events == ["freeze"]
        assert capture_still(Capture(motor), motor) is not None
        assert motor.frozen
    assert motor.events == ["freeze", "unfreeze"]


@pytest.mark.parametrize("failure", ["missing_frame", "create_raises", "init_raises", "init_false"])
def test_tracking_failure_releases_only_its_lease(monkeypatch, failure):
    motor = Motor()
    tracker = tracker_service.TrackerService()
    frame = None if failure == "missing_frame" else np.zeros((2, 2, 3), dtype=np.uint8)
    cap = SimpleNamespace(
        last_frame_ts=time.monotonic(), last_frame=frame,
        acquire_consumer=lambda: None, release_consumer=lambda: None,
    )
    # Avoid waiting for the existing no-frame timeout.
    if frame is None:
        ticks = iter([0.0, 2.0])
        monkeypatch.setattr(tracker_service.time, "monotonic", lambda: next(ticks))

    def create():
        if failure == "create_raises":
            raise RuntimeError("tracker factory failed")
        return object()

    def init(*args):
        if failure == "init_raises":
            raise RuntimeError("tracker init failed")
        return False

    monkeypatch.setattr(tracker_service, "create_tracker", create)
    monkeypatch.setattr(tracker_service, "vit_init", init)
    with freeze_lease(motor):
        if failure == "create_raises":
            with pytest.raises(RuntimeError, match="tracker factory failed"):
                tracker._start_locked((0, 0, 1, 1), "cup", cap, motor)
        else:
            assert not tracker._start_locked((0, 0, 1, 1), "cup", cap, motor)
        assert motor.frozen
        assert motor.events == ["freeze"]
    assert motor.events == ["freeze", "unfreeze"]


def test_failed_freeze_does_not_leak_ownership():
    motor = Motor()
    original = motor.freeze

    def fail():
        raise RuntimeError("freeze failed")

    motor.freeze = fail
    with pytest.raises(RuntimeError, match="freeze failed"):
        with freeze_lease(motor):
            pytest.fail("body must not run")
    assert motor.events == []
    motor.freeze = original
    with freeze_lease(motor):
        assert motor.frozen
    assert motor.events == ["freeze", "unfreeze"]


def test_snapshot_keeps_best_effort_on_freeze_failure():
    motor = Motor()
    motor.freeze = lambda: (_ for _ in ()).throw(RuntimeError("unavailable"))
    frame = object()
    cap = SimpleNamespace(
        last_frame_ts=time.monotonic(), last_frame=frame,
        acquire_consumer=lambda: None, release_consumer=lambda: None,
    )
    assert capture_still(cap, motor) is frame
    assert motor.events == []


def test_distinct_services_have_independent_ownership():
    first, second = Motor(), Motor()
    with freeze_lease(first):
        with freeze_lease(second):
            assert first.frozen and second.frozen
        assert first.frozen and not second.frozen
    assert not first.frozen
