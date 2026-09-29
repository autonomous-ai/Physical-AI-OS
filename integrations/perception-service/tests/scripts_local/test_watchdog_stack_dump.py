"""run-with-restart.sh asks a frozen child for a stack dump before SIGKILL (#530)."""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")

_FROZEN_CHILD = (
    "import sys, time; from core.stackdump import install_stack_dump; "
    "install_stack_dump('frozen', sys.argv[1]); time.sleep(600)"
)


def test_watchdog_dumps_stack_before_sigkill(tmp_path: Path):
    logs = tmp_path / "logs"
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "PROBE_GRACE": "0",
        "PROBE_INTERVAL": "1",
        "PROBE_TIMEOUT": "1",
        "PROBE_FAILURES": "2",
        "STACK_DUMP_WAIT": "1",
    }
    wrapper = subprocess.Popen(
        [
            "bash", str(ROOT / "scripts/run-with-restart.sh"),
            "--cooldown", "60",
            "--probe-url", "http://127.0.0.1:9/livez",   # nothing listens: always fails
            "--log-dir", str(logs),
            "--", sys.executable, "-c", _FROZEN_CHILD, str(tmp_path),
        ],
        env=env,
        start_new_session=True,
    )
    try:
        watchdog = logs / "watchdog.log"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if watchdog.exists() and "SIGKILL" in watchdog.read_text():
                break
            time.sleep(0.5)
        text = watchdog.read_text()
        assert "SIGUSR1" in text
        assert text.index("SIGUSR1") < text.index("SIGKILL")
        assert "most recent call first" in (tmp_path / "frozen-stack.log").read_text()
    finally:
        os.killpg(wrapper.pid, signal.SIGKILL)
        wrapper.wait(10)
