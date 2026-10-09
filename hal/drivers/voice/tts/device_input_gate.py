"""Bounded speech deferral while an explicit device capture owns the microphone."""

from collections import deque
from contextlib import contextmanager
from functools import wraps
import inspect
import logging
import threading

logger = logging.getLogger("hal.tts")


class PassiveSpeechSuppressed(RuntimeError):
    """A passive announcement lost admission to an active user capture."""

    reported = False


def device_speech(*, defer=True):
    """Reserve admission around existing entry points without changing idle policy."""
    def decorate(method):
        signature = inspect.signature(method)

        @wraps(method)
        def wrapped(self, *args, **kwargs):
            turn_valid = kwargs.pop("_device_turn_valid", None)

            def invoke():
                if turn_valid is not None and not turn_valid():
                    # This turn was accepted and later cancelled. Consume its
                    # deferred item without speech or an unspoken-main fallback.
                    return True
                try:
                    return method(self, *args, **kwargs)
                except PassiveSpeechSuppressed as error:
                    # Nested cache admission and deferred replay must report once,
                    # after the method has released its capture/queue locks.
                    if not error.reported:
                        error.reported = True
                        values = signature.bind(self, *args, **kwargs).arguments
                        logger.info("TTS passive speech suppressed -- user capture active")
                        self._report_unspoken_reply(values.get("text", ""),
                                                    values.get("realtime_feedback", False))
                    raise

            gate = getattr(self, "_device_input_gate", None)
            if gate is None:
                return invoke()
            arguments = signature.bind(self, *args, **kwargs).arguments
            if arguments.get("prerender", False):
                return method(self, *args, **kwargs)
            # Reject known-ineligible requests before promising queued acceptance.
            preview = arguments.get("preview")
            if not (self.available or (preview is not None and self._sd is not None)):
                return False
            owner = (f"run:{arguments['turn_id']}" if arguments.get("turn_id")
                     else arguments.get("owner", ""))
            if self._speaker_muted():
                self._note_speech_muted(owner)
                return False
            if owner and self._owner_suppressed(owner):
                self._report_unspoken_reply(arguments.get("text", ""),
                                            arguments.get("realtime_feedback", False))
                return False
            optional = (arguments.get("interruptible", False)
                        and not arguments.get("realtime_feedback", False)
                        and not arguments.get("realtime_reply", False))
            text = arguments.get("text", "")
            report = lambda: self._report_unspoken_reply(
                text, arguments.get("realtime_feedback", False),
            )
            return gate.submit(invoke, text,
                               can_defer=defer and not optional, report=report)
        return wrapped
    return decorate


class DeviceInputGate:
    """One FIFO replay worker; no audio/network work while holding the gate lock."""

    def __init__(self, busy, *, max_items=32, max_bytes=65536):
        self.lock = threading.RLock()
        self._changed = threading.Condition(self.lock)
        self._busy = busy
        self._tokens = set()
        self._admissions = 0
        self._generation = 0
        self._pending = deque()
        self._bytes = 0
        self._max_items = max_items
        self._max_bytes = max_bytes
        self._worker = None
        self._replaying = False
        self._closed = False
        self._local = threading.local()

    def begin(self):
        with self.lock:
            if self._closed or self._admissions or self._busy():
                return None
            token = object()
            self._tokens.add(token)
            return token

    def end(self, token):
        with self._changed:
            self._tokens.discard(token)
            self._changed.notify_all()

    def cancel(self):
        with self._changed:
            self._generation += 1
            # A synchronous preemption stops older speech, not its own admission.
            if hasattr(self._local, "generation"):
                self._local.generation = self._generation
            cleared = len(self._pending)
            self._pending.clear()
            self._bytes = 0
            self._changed.notify_all()
        if cleared:
            logger.info("Device capture speech deferral cancelled %d queued item(s)", cleared)

    def close(self, timeout=1.0):
        """Dispose the gate using its existing worker, never a shutdown thread."""
        with self._changed:
            self._closed = True
            self._generation += 1
            self._pending.clear()
            self._bytes = 0
            self._tokens.clear()
            worker = self._worker
            self._changed.notify_all()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)
        return worker is None or not worker.is_alive()

    def current_valid(self):
        """Caller holds lock when this check guards a playback-state mutation."""
        return (not self._closed
                and getattr(self._local, "generation", self._generation) == self._generation)

    @contextmanager
    def _lease(self, generation):
        self._local.generation = generation
        try:
            yield
        finally:
            del self._local.generation
            with self._changed:
                self._admissions -= 1
                self._changed.notify_all()

    def submit(self, invoke, text, *, can_defer=True, report=lambda: None):
        # speak's cache hit delegates to speak_cached under the same admission.
        if hasattr(self._local, "generation"):
            return invoke() if self.current_valid() else False
        with self._changed:
            if self._closed:
                return False
            generation = self._generation
            if self._tokens or self._pending or self._replaying:
                if not can_defer:
                    return False
                size = len(text.encode("utf-8"))
                if len(self._pending) >= self._max_items or self._bytes + size > self._max_bytes:
                    logger.warning("Device capture speech deferral full; rejecting request")
                    return False
                self._pending.append((invoke, size, generation, report))
                self._bytes += size
                if self._worker is None:
                    self._worker = threading.Thread(target=self._run, daemon=True,
                                                    name="tts-device-deferred")
                    self._worker.start()
                self._changed.notify_all()
                return True
            self._admissions += 1
        with self._lease(generation):
            return invoke()

    def _run(self):
        while True:
            with self._changed:
                while self._tokens or self._admissions or self._busy() or not self._pending:
                    if self._closed or not self._pending:
                        self._worker = None
                        return
                    # Existing TTS finalizers do not all emit a common idle event.
                    # This bounded idle wait does not create additional workers.
                    self._changed.wait(timeout=0.02 if self._pending else None)
                invoke, size, generation, report = self._pending.popleft()
                self._bytes -= size
                self._admissions += 1
                self._replaying = True
            try:
                self._replay(invoke, generation, report)
            finally:
                with self._changed:
                    self._replaying = False
                    self._changed.notify_all()

    def _replay(self, invoke, generation, report):
        with self._lease(generation):
            try:
                if not self.current_valid():
                    return
                if invoke():
                    return
                logger.warning("Deferred device speech could not start at replay")
            except PassiveSpeechSuppressed:
                # invoke already reported this as unspoken, not a transport error.
                return
            except Exception:
                logger.exception("Deferred device speech replay failed")
            report()
