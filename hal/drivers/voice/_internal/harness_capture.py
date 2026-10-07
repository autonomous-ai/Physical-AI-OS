"""Thread-safe tap-to-record ownership for the existing voice capture thread."""

from dataclasses import dataclass, field
import threading

from hal import config


def device_manual_mode(snapshot):
    """Device taps never steal capture from Harness or an unknown route."""
    return (
        config.VOICE_INPUT_MODE == "tap_to_talk"
        and snapshot.get("enabled") is False
        and not snapshot.get("unavailable", False)
        and type(snapshot.get("generation")) is int
        and snapshot["generation"] >= 0
    )


def device_snapshot(snapshot):
    """Tag an explicit local capture while preserving the OS routing generation."""
    return dict(snapshot, deviceInputMode="tap_to_talk")


def same_target(left, right):
    """Never move recorded speech across a mode, connection, or focus change."""
    if left.get("deviceInputMode") == "tap_to_talk":
        return (device_manual_mode(right)
                and left.get("generation") == right.get("generation"))
    return (
        right.get("enabled") is True
        and right.get("focusAvailable") is True
        and not right.get("unavailable")
        and all(left.get(key) == right.get(key) for key in (
            "generation", "machineId", "agentId", "focusRevision",
        ))
    )


@dataclass
class Capture:
    snapshot: dict
    finished: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    claimed: bool = False


class HarnessCapture:
    def __init__(self):
        self._lock = threading.Lock()
        self._capture = None

    @property
    def active(self):
        with self._lock:
            return self._capture is not None

    def start(self, snapshot):
        with self._lock:
            if self._capture is not None or not same_target(snapshot, snapshot):
                return False
            self._capture = Capture(dict(snapshot))
            return True

    def finish(self):
        with self._lock:
            if self._capture is None or self._capture.cancelled.is_set():
                return False
            self._capture.finished.set()
            return True

    def cancel(self):
        with self._lock:
            if self._capture is not None:
                self._capture.cancelled.set()
                self._capture = None

    def claim(self, snapshot):
        with self._lock:
            capture = self._capture
            if capture is None:
                return None
            if not same_target(capture.snapshot, snapshot):
                capture.cancelled.set()
                self._capture = None
                return None
            if capture.claimed:
                return None
            capture.claimed = True
            return capture

    def release(self, capture):
        with self._lock:
            if self._capture is capture:
                self._capture = None
