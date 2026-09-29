"""A frozen server leaves a stack dump before the watchdog kills it (#530)."""

import faulthandler
import os
import signal
import time
from pathlib import Path

from core.stackdump import install_stack_dump, stack_dump_name


def test_sigusr1_writes_all_thread_stacks(tmp_path: Path):
    path = install_stack_dump("unit", str(tmp_path))
    try:
        os.kill(os.getpid(), signal.SIGUSR1)
        time.sleep(0.2)
    finally:
        faulthandler.unregister(signal.SIGUSR1)
    text = path.read_text()
    assert path == tmp_path / "unit-stack.log"
    assert f"--- unit pid={os.getpid()} started " in text
    assert "most recent call first" in text


def test_name_follows_the_slot_log_dir():
    assert stack_dump_name("/workspace/logs/dlserver", "dlserver") == "dlserver"
    assert stack_dump_name("/workspace/logs/dlserver-8002", "dlserver") == "dlserver-8002"
    assert stack_dump_name("/workspace/logs/lbserver/", "lbserver") == "lbserver"
    assert stack_dump_name(None, "lbserver") == "lbserver"
