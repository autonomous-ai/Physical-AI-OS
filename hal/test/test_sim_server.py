"""Off-device runtime proof for the mock body."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class TestSimServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.logs = tempfile.TemporaryDirectory(prefix="autonomous-hal-sim-")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]

        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(REPO_ROOT),
                "HAL_MODE": "developer",
                "HAL_SIMULATE": "1",
                "HAL_BOARD": "sim",
                "DEVICE_TYPE": "sim",
                "HAL_LOG_DIR": cls.logs.name,
                "OS_CONFIG_PATH": str(Path(cls.logs.name) / "missing-config.json"),
            }
        )
        cls.server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "hal.server:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(cls.port),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                output = cls.server.stdout.read() if cls.server.stdout else ""
                raise RuntimeError(f"sim HAL exited during boot:\n{output}")
            try:
                cls._request("/health")
                return
            except urllib.error.URLError:
                time.sleep(0.1)
        cls.tearDownClass()
        raise TimeoutError("sim HAL did not become healthy within 15 seconds")

    @classmethod
    def tearDownClass(cls):
        server = getattr(cls, "server", None)
        if server and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        logs = getattr(cls, "logs", None)
        if logs:
            logs.cleanup()

    @classmethod
    def _request(cls, path: str, method: str = "GET", body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{cls.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        # Longer than any single move: the safety gate stretches /servo/move to 1.5s.
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)

    def test_declared_mock_body_boots_without_host_peripherals(self):
        status, health = self._request("/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["servo"])
        self.assertFalse(health["led"])
        self.assertFalse(health["camera"])
        self.assertFalse(health["audio"])

        status, device = self._request("/device")
        self.assertEqual(status, 200)
        self.assertEqual(device["id"], "sim")
        self.assertEqual(device["board"], "sim")
        self.assertEqual(device["drivers"]["motion"], "mock")

    def test_motion_skill_http_path_is_safety_gated_and_stoppable(self):
        status, moved = self._request(
            "/servo/move",
            method="POST",
            body={"positions": {"base_yaw.pos": 90}, "duration": 0.1},
        )
        self.assertEqual(status, 200)
        self.assertEqual(moved["status"], "ok")
        self.assertEqual(moved["duration"], 1.5)

        status, aimed = self._request(
            "/servo/aim",
            method="POST",
            body={"direction": "left", "duration": 0.2},
        )
        self.assertEqual(status, 200)
        from hal.presets import AIM_PRESETS

        self.assertAlmostEqual(
            aimed["positions"]["base_yaw.pos"],
            AIM_PRESETS["left"]["base_yaw.pos"],
            places=3,
        )

        status, stopped = self._request("/servo/stop", method="POST", body={})
        self.assertEqual(status, 200)
        self.assertEqual(stopped["status"], "ok")

        status, position = self._request("/servo/position")
        self.assertEqual(status, 200)
        self.assertAlmostEqual(
            position["positions"]["base_yaw.pos"],
            AIM_PRESETS["left"]["base_yaw.pos"],
            places=3,
        )
