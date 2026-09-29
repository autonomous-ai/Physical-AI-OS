"""A RotatingFileHandler that survives a failed write.

The stock handler never reopens a dead stream: once a write raises, that log is
silent until the process restarts. On 2026-08-17 lbserver.log stopped at 07:59:35
and stayed dead for the rest of the day while its mtime kept advancing -- writes
were still being attempted, still failing, and nothing ever tried again.

The trigger was storage-side (an EIO on a live, non-deleted file on MooseFS) and
cannot be prevented from here. What can be fixed is the permanence: reopen, and
if the file itself is unwritable, roll over to a fresh one.

This is filesystem-agnostic on purpose -- it covers a transient ENOSPC on local
disk just as well, which matters on a container whose root is 97% full.

Recovery only helps with writes that *fail*. On 2026-09-28 (#530) a write that
*hung* on the same MooseFS volume froze lbserver's event loop for ~3 minutes:
every request, WebSocket and /livez stalled behind one logger.info(). So file
handlers are now wrapped with queued(): the caller only enqueues, and a
background thread does the writing. A stuck filesystem call blocks that thread,
never the event loop.
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

    Failure handling hangs off handleError() because logging.Handler.emit()
    already swallows exceptions and routes them there -- so super().emit() never
    raises and cannot be used to detect the fault.
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
        # While muted, drop records rather than retry a known-broken stream for
        # every one. Retrying produced ~2 KB/s of tracebacks for 40 minutes in the
        # 2026-08-17 incident.
        if time.monotonic() < self._muted_until:
            return
        self._errored = False
        super().emit(record)
        # Only a write that did NOT route through handleError counts as recovery.
        # Checking `self.stream is not None` instead would clear the counter after
        # every reopen, so the rollover threshold would never be reached.
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
                # Reopening the same path keeps failing if the file itself is the
                # problem, so take a fresh inode -- but only once per fault, or a
                # persistent fault would churn through every backup and destroy
                # the history we are trying to keep.
                self.doRollover()
                self._rolled_over_for_fault = True
            else:
                self.stream = self._open()
                if self._consecutive_failures >= self.failures_before_rollover:
                    # A fresh file did not help either; stop trying for a while.
                    self._muted_until = time.monotonic() + self.retry_after
        except Exception:
            # Recovery itself failed: back off so we neither spin nor flood.
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

    The stock QueueHandler with a bounded queue raises queue.Full into
    handleError(), which prints a traceback per record -- the flood the handler
    above exists to avoid. Here a full queue just counts the loss, and the
    writer reports the total once it catches up.
    """

    def __init__(self, maxsize: int = DEFAULT_QUEUE_SIZE) -> None:
        super().__init__(queue.Queue(maxsize))
        self._dropped = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        # Runs under self.lock (Handler.handle holds it around emit).
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self._dropped += 1

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

    def stop(self, timeout: float = 2.0) -> None:
        # The stock stop() joins with no timeout: if the writer is stuck in a
        # filesystem call, a clean shutdown would hang with it. The thread is a
        # daemon, so giving up here lets the process exit anyway.
        thread = self._thread
        if thread is None:
            return
        try:
            self.queue.put_nowait(self._sentinel)
        except queue.Full:
            pass
        thread.join(timeout)
        self._thread = None


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
    while _listeners:
        _listeners.pop().stop(timeout)


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
