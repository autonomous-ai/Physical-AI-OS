"""Thread-safe tap-to-record ownership for the existing voice capture thread."""

from dataclasses import dataclass, field
import threading


def same_target(left, right):
    """Never move recorded speech across a mode, connection, or focus change."""
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
    reservation: object = None


class HarnessCapture:
    def __init__(self, target_matches=same_target):
        self._target_matches = target_matches
        self._lock = threading.Lock()
        self._capture = None

    @property
    def active(self):
        with self._lock:
            return self._capture is not None

    def start(self, snapshot, *, reservation=None):
        with self._lock:
            if self._capture is not None or not self._target_matches(snapshot, snapshot):
                return False
            self._capture = Capture(dict(snapshot), reservation=reservation)
            if reservation is not None:
                self._capture.cancelled = reservation.cancelled
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
            if not self._target_matches(capture.snapshot, snapshot):
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
