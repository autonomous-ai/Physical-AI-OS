"""Stack dump on SIGUSR1, for a server the watchdog is about to kill.

On 2026-09-28 (#530) lbserver froze for ~3 minutes and was SIGKILLed with no
record of where it was stuck, so the cause could only be inferred from log
timestamps. faulthandler dumps every thread's Python stack from a C signal
handler, which works while the event loop is blocked.

The dump goes to local /tmp, not the log volume: a hung log volume is the
leading suspect, and a dump written there would hang with it.
"""

from __future__ import annotations

import faulthandler
import os
import signal
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

# faulthandler writes to the file's fd; the object must outlive the registration.
_stack_file: TextIO | None = None


def stack_dump_name(log_dir: str | None, default: str) -> str:
    """Name the dump after the --log-dir basename, like the pid files.

    Two dlserver slots run side by side (#519) with log dirs dlserver/ and
    dlserver-8002/, and only one instance may hold a log dir, so the basename
    is unique per running process.
    """
    return Path(log_dir).name if log_dir else default


def install_stack_dump(name: str, directory: str | None = None) -> Path:
    """Dump all thread stacks to <directory>/<name>-stack.log on SIGUSR1."""
    global _stack_file
    path = Path(directory or tempfile.gettempdir()) / f"{name}-stack.log"
    _stack_file = open(path, "a")  # noqa: SIM115 -- held for the process lifetime
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    _stack_file.write(f"--- {name} pid={os.getpid()} started {started} ---\n")
    _stack_file.flush()
    faulthandler.register(signal.SIGUSR1, file=_stack_file, all_threads=True)
    return path
