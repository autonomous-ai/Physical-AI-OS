"""Device tap-to-talk controller over the shared, serialized capture owner."""

import threading
import logging
import time
from contextlib import ExitStack

from hal.drivers.voice._internal.harness_voice import read_voice_mode
from hal.drivers.voice._internal.input_policy import (
    device_manual_mode, device_snapshot, same_capture_target,
)


logger = logging.getLogger("hal.voice")


class DeviceInputLease:
    """Keep each TTS guard's owner across a transactional service replacement."""

    def __init__(self):
        self.lock = threading.RLock()
        self.owner = None
        self._guards = []

    @classmethod
    def begin(cls, owner):
        lease = cls()
        return lease if lease.replace(owner) else None

    def replace(self, owner):
        with self.lock:
            if owner is not None and not any(previous is owner for previous, _ in self._guards):
                token = owner.begin_device_input()
                if token is None:
                    return False
                self._guards.append((owner, token))
            self.owner = owner
            return True

    def close(self):
        with self.lock:
            guards, self._guards = self._guards, []
            for owner, token in guards:
                owner.end_device_input(token)

    def play_device_capture_chime(self, *, finished=False):
        with self.lock:
            if self.owner is not None:
                self.owner.play_device_capture_chime(finished=finished)

    @property
    def last_spoken_text(self):
        with self.lock:
            return getattr(self.owner, "last_spoken_text", "")


class DeviceTapInput:
    def __init__(self, capture, start_capture, *, read_mode=None, turn_queue=None,
                 begin_input=None, end_input=None):
        self._capture = capture
        # The pipeline supplies its existing privacy/playback admission guard.
        self._start_capture = start_capture
        self._read_mode = read_mode or read_voice_mode
        self._mode = {}
        self._wake = threading.Event()
        self._turn_queue = turn_queue
        self._begin_input = begin_input
        self._end_input = end_input
        self._lock = threading.Lock()
        self._reservations = {}

    def observe(self, mode):
        """Cache authoritative routing for nonblocking hardware edge recognition."""
        self._mode = dict(mode)
        if self._turn_queue is not None:
            with self._lock:
                self._turn_queue.cancel_invalid(lambda snapshot: same_capture_target(snapshot, mode))
                self._release_unclaimed_cancelled()

    @property
    def enabled(self):
        return device_manual_mode(self._mode)

    @property
    def active(self):
        return self._capture.active

    def start(self, *, after_ms=0):
        # Never authorize a new recording from the cached hardware-edge state.
        mode = self._read_mode()
        self.observe(mode)
        if not device_manual_mode(mode):
            return False
        snapshot = device_snapshot(mode)
        snapshot["capturedAtMs"] = max(int(time.time() * 1000), after_ms + 1)
        if self._turn_queue is None:
            accepted = self._start_capture(snapshot)
        else:
            with self._lock:
                ticket = self._turn_queue.reserve(snapshot)
                if ticket is None:
                    logger.info("Device tap rejected before recording: finalization capacity full")
                    return False
                token = None
                accepted = False
                try:
                    token = self._begin_input() if self._begin_input else None
                    if self._begin_input and token is None:
                        return False
                    accepted = self._start_capture(snapshot, reservation=ticket)
                    if accepted:
                        self._reservations[ticket] = (token, False)
                finally:
                    if not accepted:
                        self._turn_queue.release(ticket)
                        if self._end_input and token is not None:
                            self._end_input(token)
        if accepted:
            self._wake.set()
        return accepted

    def wait_for_capture(self, timeout=0.1):
        """Wake idle capture promptly while retaining periodic route checks.

        Clear before inspecting the authoritative capture state: a start before
        clear remains visible as active; a later start signals the event.
        """
        self._wake.clear()
        if self._capture.active:
            return True
        return self._wake.wait(timeout)

    def finish(self):
        return self._capture.finish()

    def cancel(self):
        with self._lock:
            self._capture.cancel()
            if self._turn_queue is not None:
                self._turn_queue.cancel_all()
                self._release_unclaimed_cancelled()
        self._wake.set()

    def claim(self, capture):
        """Transfer the admission TTS guard to the mic worker atomically."""
        with self._lock:
            ticket = capture.reservation
            record = self._reservations.get(ticket)
            if record is None or capture.cancelled.is_set():
                return False
            self._reservations[ticket] = (record[0], True)
            return True

    def release(self, capture):
        """Release the guard only after the caller has closed the microphone."""
        with self._lock:
            record = self._reservations.pop(capture.reservation, None)
            if record is not None and self._end_input and record[0] is not None:
                self._end_input(record[0])

    def input_lease(self, capture):
        with self._lock:
            record = self._reservations.get(capture.reservation)
            return record[0] if record is not None else None

    def replace_input(self, factory, install):
        """Quiesce cue writes while replacing output and moving active guards."""
        with self._lock, ExitStack() as stack:
            leases = [(ticket, record[0]) for ticket, record in self._reservations.items()]
            for _, lease in leases:
                stack.enter_context(lease.lock)
            replacement = factory()
            for ticket, lease in leases:
                if not lease.replace(replacement):
                    self._turn_queue.release(ticket)
            install(replacement)
            self._release_unclaimed_cancelled()
            return replacement

    def _release_unclaimed_cancelled(self):
        for ticket, (token, claimed) in list(self._reservations.items()):
            if ticket.cancelled.is_set() and not claimed:
                del self._reservations[ticket]
                if self._end_input and token is not None:
                    self._end_input(token)
