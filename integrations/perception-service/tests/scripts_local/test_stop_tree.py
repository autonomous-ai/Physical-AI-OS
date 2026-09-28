"""scripts/stop-tree.sh must stop exactly one instance when two share a name."""

import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STOP_TREE = ROOT / "scripts" / "stop-tree.sh"
pytestmark = pytest.mark.skipif(shutil.which("pgrep") is None, reason="needs pgrep")


def _fake(cmdline: str) -> subprocess.Popen:
    """A sleeping process whose command line reads like `cmdline` (what pgrep -f sees)."""
    return subprocess.Popen(
        ["bash", "-c", f'exec -a "{cmdline}" sleep 300'], start_new_session=True
    )


def _stop(log_dir: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    # Port 59997 is unused: only the log-dir patterns can find the fakes.
    return subprocess.run(
        ["bash", str(STOP_TREE), "dlserver", "59997",
         str(tmp_path / "none-w.pid"), str(tmp_path / "none.pid"), str(log_dir)],
        capture_output=True, text=True, timeout=60,
    )


@pytest.fixture()
def slots(tmp_path):
    a, b = tmp_path / "dlserver", tmp_path / "dlserver-8002"
    procs = {
        "a_child": _fake(f"python -m dlserver --host 127.0.0.1 --port 8001 --log-dir {a}"),
        "a_wrapper": _fake(f"bash scripts/run-with-restart.sh --log-dir {a} -- python -m dlserver --port 8001 --log-dir {a}"),
        "b_child": _fake(f"python -m dlserver --host 127.0.0.1 --port 8002 --log-dir {b}"),
        "b_wrapper": _fake(f"bash scripts/run-with-restart.sh --log-dir {b} -- python -m dlserver --port 8002 --log-dir {b}"),
    }
    time.sleep(0.3)
    yield a, b, procs
    for p in procs.values():
        p.kill()
        p.wait()


def _dead(p: subprocess.Popen) -> bool:
    try:
        p.wait(timeout=5)
        return True
    except subprocess.TimeoutExpired:
        return False


def test_stopping_slot_b_leaves_slot_a(slots, tmp_path):
    a, b, procs = slots
    r = _stop(b, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _dead(procs["b_child"]) and _dead(procs["b_wrapper"])
    assert procs["a_child"].poll() is None and procs["a_wrapper"].poll() is None


def test_slot_a_log_dir_is_not_a_prefix_of_slot_b(slots, tmp_path):
    a, b, procs = slots
    r = _stop(a, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _dead(procs["a_child"]) and _dead(procs["a_wrapper"])
    assert procs["b_child"].poll() is None and procs["b_wrapper"].poll() is None
