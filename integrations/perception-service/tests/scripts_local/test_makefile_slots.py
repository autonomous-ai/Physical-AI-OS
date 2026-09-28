"""Makefile slot variables: one dlserver slot per port, defaulting to the active one."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="needs make")


def _dry(tmp_path: Path, *args: str) -> str:
    r = subprocess.run(
        ["make", "-n", "--no-print-directory", "-C", str(ROOT),
         f"RUN_DIR={tmp_path}", f"LOG_ROOT={tmp_path}/logs", *args],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_default_slot_keeps_legacy_paths(tmp_path):
    out = _dry(tmp_path, "start-runpod-dlserver-slot")
    assert f"--pid-file {tmp_path}/dlserver.pid " in out
    assert f"--wrapper-pid-file {tmp_path}/dlserver-wrapper.pid " in out
    assert f"--log-dir {tmp_path}/logs/dlserver " in out
    assert "--port 8001 " in out
    assert f"{tmp_path}/dlserver.rev" in out


def test_second_slot_gets_its_own_paths(tmp_path):
    out = _dry(tmp_path, "start-runpod-dlserver-slot", "DLSERVER_PORT=8002")
    assert f"--pid-file {tmp_path}/dlserver-8002.pid " in out
    assert f"--wrapper-pid-file {tmp_path}/dlserver-8002-wrapper.pid " in out
    assert f"--log-dir {tmp_path}/logs/dlserver-8002 " in out
    assert "--port 8002 " in out


def test_slot_start_skips_install(tmp_path):
    assert "pip install" not in _dry(tmp_path, "start-runpod-dlserver-slot", "DLSERVER_PORT=8002")


def test_default_port_follows_state_file(tmp_path):
    (tmp_path / "dlserver-active").write_text("http://127.0.0.1:8002\n")
    out = _dry(tmp_path, "stop-runpod-dlserver")
    assert (
        f"stop-tree.sh dlserver 8002 {tmp_path}/dlserver-8002-wrapper.pid "
        f"{tmp_path}/dlserver-8002.pid {tmp_path}/logs/dlserver-8002"
    ) in out


def test_garbage_state_file_falls_back_to_slot_a(tmp_path):
    (tmp_path / "dlserver-active").write_text("nonsense\n")
    assert "stop-tree.sh dlserver 8001 " in _dry(tmp_path, "stop-runpod-dlserver")


def test_lbserver_gets_state_file_and_log_dir(tmp_path):
    out = _dry(tmp_path, "start-runpod-lbserver")
    assert f"LB__STATE_FILE={tmp_path}/dlserver-active " in out
    assert f"--log-dir {tmp_path}/logs/lbserver " in out


def test_print_dlserver_pid_follows_state_file(tmp_path):
    (tmp_path / "dlserver-active").write_text("http://127.0.0.1:8002\n")
    r = subprocess.run(
        ["make", "-s", "--no-print-directory", "-C", str(ROOT), f"RUN_DIR={tmp_path}", "print-dlserver-pid"],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == f"{tmp_path}/dlserver-8002.pid"
