"""Tests for hal.drivers.base.ServiceBase — the single-slot event mailbox."""

import threading
import time

from hal.drivers.base import ServiceBase


class _RecordingService(ServiceBase):
    """Records payloads; handler blocks until `proceed` is set."""

    def __init__(self):
        super().__init__("test")
        self.handled: list = []
        self.in_handler = threading.Event()
        self.proceed = threading.Event()

    def handle_event(self, event_type, payload):
        self.in_handler.set()
        self.proceed.wait(timeout=2.0)
        self.handled.append(payload)


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_dispatch_then_handle():
    svc = _RecordingService()
    svc.proceed.set()
    svc.start()
    try:
        svc.dispatch("evt", "a")
        assert _wait_for(lambda: svc.handled == ["a"])
    finally:
        svc.stop(timeout=1.0)


def test_dispatch_during_handling_is_not_lost():
    svc = _RecordingService()
    svc.start()
    try:
        svc.dispatch("evt", "a")
        assert svc.in_handler.wait(timeout=2.0)  # worker is inside handle_event("a")

        svc.dispatch("evt", "b")  # lands while "a" is being handled
        svc.proceed.set()

        assert _wait_for(lambda: svc.handled == ["a", "b"]), (
            f"second event lost: handled={svc.handled}"
        )
    finally:
        svc.stop(timeout=1.0)
