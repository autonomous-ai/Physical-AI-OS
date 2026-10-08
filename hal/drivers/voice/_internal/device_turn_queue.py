"""Bounded, ordered finalization after a device recording releases its mic."""

from collections import deque
from dataclasses import dataclass, field
import logging
import threading


logger = logging.getLogger("hal.voice")


@dataclass(eq=False)
class DeviceTurnReservation:
    snapshot: dict
    cancelled: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    _run: object = None
    _cleanup: object = None


class DeviceTurnQueue:
    """Reserve capacity before accepting speech; finalize on one FIFO worker.

    A reservation includes active recording, queued work and running work. The
    caller must reserve before its ready cue and release an abandoned recording.
    ``submit`` transfers ownership only on True; cleanup then runs exactly once,
    including cancelled jobs and failed finalizers. On False the caller still
    owns all resources. Finalizers must honor the cancellation event immediately
    before dispatch and use bounded network waits: Python cannot kill a thread.

    This queue does not authorize routing or own the microphone. Bind the
    reservation's cancellation event to the capture and call ``cancel_all`` on
    privacy/shutdown, or ``cancel_invalid`` after an authoritative route read.
    """

    def __init__(self, capacity=2):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._condition = threading.Condition()
        self._pending = deque()
        self._closed = False
        self._worker = None

    @property
    def pending(self):
        with self._condition:
            return len(self._pending)

    def reserve(self, snapshot, *, cancelled=None):
        with self._condition:
            if (self._closed or len(self._pending) >= self._capacity
                    or (cancelled is not None and cancelled.is_set())):
                return None
            ticket = DeviceTurnReservation(dict(snapshot))
            if cancelled is not None:
                ticket.cancelled = cancelled
            self._pending.append(ticket)
            return ticket

    def submit(self, ticket, run, cleanup):
        """Queue run(cancelled_event), then cleanup(), in reservation order."""
        if not callable(run) or not callable(cleanup):
            raise TypeError("run and cleanup must be callable")
        with self._condition:
            if (self._closed or ticket not in self._pending
                    or ticket._run is not None or ticket.cancelled.is_set()):
                return False
            ticket._run = run
            ticket._cleanup = cleanup
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._work, name="device-turn-finalizer", daemon=True,
                )
                self._worker.start()
            self._condition.notify_all()
            return True

    def release(self, ticket):
        """Abandon an unsubmitted reservation; submitted work keeps its slot."""
        with self._condition:
            self._cancel_locked(ticket)
            self._condition.notify_all()

    def _cancel_locked(self, ticket):
        if ticket not in self._pending:
            return
        ticket.cancelled.set()
        if ticket._run is None:
            self._pending.remove(ticket)
            ticket.done.set()

    def cancel_all(self):
        with self._condition:
            for ticket in list(self._pending):
                self._cancel_locked(ticket)
            self._condition.notify_all()

    def cancel_invalid(self, valid):
        """Cancel old route snapshots without holding the queue lock in valid."""
        with self._condition:
            tickets = list(self._pending)
        for ticket in tickets:
            if not valid(dict(ticket.snapshot)):
                self.release(ticket)

    def shutdown(self, timeout=1.0):
        """Reject new work, cancel outstanding work, wait at most timeout."""
        with self._condition:
            self._closed = True
            for ticket in list(self._pending):
                self._cancel_locked(ticket)
            worker = self._worker
            self._condition.notify_all()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)
        return worker is None or not worker.is_alive()

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: (
                    (self._pending and self._pending[0]._run is not None)
                    or (self._closed and not self._pending)
                ))
                if self._closed and not self._pending:
                    return
                ticket = self._pending[0]
            try:
                if not ticket.cancelled.is_set():
                    ticket._run(ticket.cancelled)
            except Exception:
                logger.exception("Device turn finalization failed")
            finally:
                try:
                    ticket._cleanup()
                except Exception:
                    logger.exception("Device turn cleanup failed")
                finally:
                    with self._condition:
                        self._pending.remove(ticket)
                        ticket._run = None
                        ticket._cleanup = None
                        ticket.done.set()
                        self._condition.notify_all()
