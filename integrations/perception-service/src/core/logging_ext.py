"""A RotatingFileHandler that survives a failed write (e.g. EIO on MooseFS, ENOSPC)."""

from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler


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
