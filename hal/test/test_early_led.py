"""Exercise the real LED router and runtime handoff without physical hardware."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("device", ["lamp", "intern-v2", "reachy-mini"])
def test_led_before_runtime_import_and_after_handoff(tmp_path, device):
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=str(root), DEVICE_TYPE=device,
               HAL_SIMULATE="1", HAL_SIM_MEDIA="virtual", HAL_BOARD="sim",
               HAL_MODE="developer", HAL_LOG_DIR=str(tmp_path / "logs"),
               HAL_USERS_DIR=str(tmp_path / "users"), HAL_STRANGERS_DIR=str(tmp_path / "strangers"),
               HAL_BT_STATE_DIR=str(tmp_path), HAL_VOLUME_STATE_PATH=str(tmp_path / "volume"),
               OS_CONFIG_PATH=str(tmp_path / "absent-config.json"))
    script = r'''
import sys
import threading
import time
from fastapi.testclient import TestClient
from hal import server
import hal.app_state as state
from hal.server_support.early_runtime import EarlyRuntime

release = threading.Event()
def load():
    assert release.wait(10)
    return server._load_runtime()

app = EarlyRuntime(server._bootstrap, load, server._cleanup_early)
with TestClient(app, client=("127.0.0.1", 12345)) as client:
    try:
        assert client.get("/health").status_code == 503
        assert client.post("/servo/move", json={"positions": {}}).status_code == 503
        has_led = "led" in server._boot.profile.declared_routes()
        if has_led:
            reply = client.post("/led/status", json={"state": "setup"})
            assert reply.status_code == 200, reply.text
            assert reply.json()["status"] == "ok", reply.text
            assert client.get("/led/color").status_code == 200
        else:
            assert client.post("/led/status", json={"state": "setup"}).status_code == 503
        owner = state.rgb_service
        for prefix in ("hal.runtime", "torch", "onnxruntime", "cv2", "sounddevice", "lerobot",
                       "hal.drivers.voice.tts", "hal.drivers.sensing"):
            assert not any(name == prefix or name.startswith(prefix + ".") for name in sys.modules), prefix
    finally:
        release.set()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        reply = client.get("/health")
        if reply.status_code == 200:
            break
        time.sleep(0.02)
    assert reply.status_code == 200, reply.text
    assert state.rgb_service is owner, "Full startup reopened the LED driver"
    assert client.get("/device").status_code == 200
    if has_led:
        assert reply.json()["led"]
        assert client.post("/led/status", json={"state": "setup"}).status_code == 200
'''
    result = subprocess.run([sys.executable, "-c", script], env=env, cwd=root,
                            capture_output=True, text=True, timeout=35)
    assert result.returncode == 0, result.stdout + result.stderr
