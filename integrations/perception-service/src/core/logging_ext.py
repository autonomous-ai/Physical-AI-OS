"""A RotatingFileHandler that survives a failed write (e.g. EIO on MooseFS, ENOSPC).

queued() wraps file handlers so a hung filesystem write blocks a background
thread, never the event loop (#530).
"""

from __future__ import annotations

import atexit
import logging
import queue
import sys
import time
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from typing import Any


class ResilientRotatingFileHandler(RotatingFileHandler):
    """Reopen on write failure; roll to a fresh file if the current one stays bad.

    Faults are detected in handleError(): Handler.emit() swallows exceptions.
    """

    def __init__(
        self,
        *args: object,
        retry_after: float = 30.0,
        failures_before_rollover: int = 3,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.retry_after = retry_after
        self.failures_before_rollover = failures_before_rollover
        self._consecutive_failures = 0
        self._muted_until = 0.0
        self._last_report = 0.0
        self._errored = False
        self._rolled_over_for_fault = False

    def emit(self, record: logging.LogRecord) -> None:
        if time.monotonic() < self._muted_until:
            return
        self._errored = False
        super().emit(record)
        # Only a write that did not route through handleError counts as recovery.
        if not self._errored and self._consecutive_failures:
            self._consecutive_failures = 0
            self._rolled_over_for_fault = False

    def handleError(self, record: logging.LogRecord) -> None:
        """Called by Handler.emit() when the write raised. Try to get writable again."""
        self._errored = True
        self._consecutive_failures += 1
        self._report(record)
        try:
            self._close_stream()
            if (
                self._consecutive_failures >= self.failures_before_rollover
                and not self._rolled_over_for_fault
            ):
                # Roll to a fresh inode only once per fault, or a persistent fault
                # would churn through every backup.
                self.doRollover()
                self._rolled_over_for_fault = True
            else:
                self.stream = self._open()
                if self._consecutive_failures >= self.failures_before_rollover:
                    self._muted_until = time.monotonic() + self.retry_after
        except Exception:
            self._close_stream()
            self._muted_until = time.monotonic() + self.retry_after

    def _close_stream(self) -> None:
        if self.stream is not None:
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def _report(self, record: logging.LogRecord) -> None:
        """Rate-limited failure notice. The stock handler prints a ~40 line
        traceback per record, which is the flood this class exists to avoid."""
        if not logging.raiseExceptions or not sys.stderr:
            return
        now = time.monotonic()
        if now - self._last_report < self.retry_after:
            return
        self._last_report = now
        exc = sys.exc_info()[1]
        try:
            sys.stderr.write(
                f"[logging] write failed on {self.baseFilename}: {exc!r} "
                f"(failure {self._consecutive_failures}; further notices "
                f"suppressed for {self.retry_after:.0f}s)\n"
            )
        except Exception:
            pass


# ~5-10 MB of records at most. Enough to ride out a multi-minute stall at
# lbserver's WebSocket log rate without letting a dead volume eat the RAM.
DEFAULT_QUEUE_SIZE = 10_000


class NonBlockingQueueHandler(QueueHandler):
    """Hand records to a background writer; drop instead of blocking when full.

    A full queue just counts the loss, and the writer reports the total once it
    catches up -- never a traceback per record, the flood the handler above
    exists to avoid.

    The queue is a SimpleQueue, not a bounded queue.Queue: queue.Queue.put()
    holds a plain (non-reentrant) Lock in Python code, so a signal handler that
    logs while the main thread is inside put() -- lbserver's SIGHUP slot switch
    -- deadlocks the event loop. SimpleQueue.put() is one C call, safe to
    re-enter from a signal handler. The size limit is enforced here instead.
    """

    def __init__(self, maxsize: int = DEFAULT_QUEUE_SIZE) -> None:
        super().__init__(queue.SimpleQueue())
        self.maxsize = maxsize
        self._dropped = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        # Runs under self.lock (Handler.handle holds it around emit). The size
        # check is approximate: the writer draining concurrently only makes room.
        if self.queue.qsize() >= self.maxsize:
            self._dropped += 1
        else:
            self.queue.put_nowait(record)

    def take_dropped(self) -> int:
        """Return and reset the drop count. Called from the writer thread."""
        with self.lock:
            dropped, self._dropped = self._dropped, 0
        return dropped


class _DrainingListener(QueueListener):
    """QueueListener that reports drops and never hangs shutdown."""

    def __init__(self, source: NonBlockingQueueHandler, *handlers: logging.Handler) -> None:
        super().__init__(source.queue, *handlers, respect_handler_level=True)
        self._source = source

    def handle(self, record: logging.LogRecord) -> None:
        dropped = self._source.take_dropped()
        if dropped:
            super().handle(_dropped_notice(dropped))
        super().handle(record)

    def stop(self, timeout: float = 2.0) -> bool:
        """Drain and stop; return False if the writer is still stuck after `timeout`."""
        # The stock stop() joins with no timeout: if the writer is stuck in a
        # filesystem call, a clean shutdown would hang with it. The thread is a
        # daemon, so giving up here lets the process exit anyway.
        thread = self._thread
        if thread is None:
            return True
        self.queue.put_nowait(self._sentinel)
        thread.join(timeout)
        self._thread = None
        return not thread.is_alive()


def _dropped_notice(count: int) -> logging.LogRecord:
    # Built through the record factory so it carries request_id like any other
    # record -- LOG_FORMAT references it and formatting would raise otherwise.
    return logging.getLogRecordFactory()(
        __name__,
        logging.WARNING,
        __file__,
        0,
        "[logging] queue full: dropped %d record(s) while the log writer was stalled",
        (count,),
        None,
    )


_listeners: list[_DrainingListener] = []


def queued(target: logging.Handler, maxsize: int = DEFAULT_QUEUE_SIZE) -> NonBlockingQueueHandler:
    """Wrap `target` so emit() only enqueues; a background thread writes through it.

    Set the formatter on `target`, not on the returned handler.
    """
    handler = NonBlockingQueueHandler(maxsize)
    # prepare() bakes this formatter's output into record.msg before `target`
    # applies the real format. Pin it: basicConfig() would otherwise give a
    # formatter-less handler BASIC_FORMAT and every line would read "INFO:name:msg".
    handler.setFormatter(logging.Formatter("%(message)s"))
    listener = _DrainingListener(handler, target)
    listener.start()
    if not _listeners:
        # Registered after logging's own atexit hook, so it runs first (LIFO):
        # queued records reach their files before logging.shutdown() closes them.
        atexit.register(stop_queued_logging)
    _listeners.append(listener)
    return handler


def stop_queued_logging(timeout: float = 2.0) -> None:
    """Flush and stop every writer started by queued(), waiting at most `timeout` each."""
    stuck = False
    while _listeners:
        stuck |= not _listeners.pop().stop(timeout)
    if stuck:
        # A writer is still inside a filesystem call, holding its target
        # handler's lock. logging.shutdown() -- the next atexit hook -- would wait
        # on that lock forever, so skip it; the daemon writer dies with the process.
        atexit.unregister(logging.shutdown)


def queued_rotating_file_handler(
    filename: str, fmt: str, maxBytes: int, backupCount: int
) -> NonBlockingQueueHandler:
    """dictConfig "()" factory: a queued ResilientRotatingFileHandler."""
    target = ResilientRotatingFileHandler(filename, maxBytes=maxBytes, backupCount=backupCount)
    target.setFormatter(logging.Formatter(fmt))
    return queued(target)


def uvicorn_file_log_config(filename: str, fmt: str) -> dict[str, Any]:
    """uvicorn log_config that routes its loggers to one queued file handler.

    "uvicorn" and "uvicorn.access" MUST share a single handler instance. Two
    RotatingFileHandlers on the same path keep independent byte counters and roll
    over independently, so one eventually unlinks the inode the other still holds
    open. On MooseFS (/workspace) writing to a deleted-but-open file returns EIO.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {
            "file": {
                "()": "core.logging_ext.queued_rotating_file_handler",
                "filename": filename,
                "fmt": fmt,
                "maxBytes": 1_048_576,
                "backupCount": 3,
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["file"], "level": "INFO", "propagate": False},
        },
    }
