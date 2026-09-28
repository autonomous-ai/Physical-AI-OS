"""End-to-end proof for the generic laptop Lamp simulator."""
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
import wave
from io import BytesIO
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class TestLampSimulationServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = tempfile.TemporaryDirectory(prefix="autonomous-lamp-sim-")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        root = Path(cls.state.name)
        env = os.environ.copy()
        env.update({
            "PYTHONPATH": str(REPO_ROOT), "HAL_MODE": "developer",
            "HAL_SIMULATE": "1", "HAL_BOARD": "sim", "DEVICE_TYPE": "lamp",
            "HAL_LOG_DIR": str(root / "logs"), "HAL_USERS_DIR": str(root / "users"),
            "HAL_STRANGERS_DIR": str(root / "strangers"), "HAL_BT_STATE_DIR": str(root),
            "HAL_VOLUME_STATE_PATH": str(root / "volume"),
            "OS_CONFIG_PATH": str(root / "missing-config.json"),
        })
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "hal.server:app", "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                output = cls.server.stdout.read() if cls.server.stdout else ""
                raise RuntimeError(f"Lamp simulator exited during boot:\n{output}")
            try:
                cls._json("/health")
                return
            except urllib.error.URLError:
                time.sleep(0.1)
        cls.tearDownClass()
        raise TimeoutError("Lamp simulator did not become healthy within 20 seconds")

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
        state = getattr(cls, "state", None)
        if state:
            state.cleanup()

    @classmethod
    def _response(cls, path: str, method: str = "GET", body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{cls.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        return urllib.request.urlopen(request, timeout=3)

    @classmethod
    def _json(cls, path: str, method: str = "GET", body: dict | None = None):
        with cls._response(path, method, body) as response:
            return response.status, json.load(response)

    def test_lamp_routes_boot_with_virtual_peripherals(self):
        status, device = self._json("/device")
        self.assertEqual(status, 200)
        self.assertEqual(device["id"], "lamp")
        self.assertEqual(device["board"], "sim")
        self.assertIn("servo", device["routes"])
        self.assertIn("led", device["routes"])
        self.assertIn("camera", device["routes"])
        self.assertIn("voice", device["routes"])

        _, health = self._json("/health")
        self.assertTrue(health["servo"])
        self.assertTrue(health["led"])
        self.assertTrue(health["camera"])
        self.assertTrue(health["audio"])
        self.assertTrue(health["sensing"])
        self.assertTrue(health["voice"])

    def test_motion_led_camera_and_virtual_audio_follow_real_http_contracts(self):
        _, moved = self._json("/servo/move", "POST", {"positions": {"base_yaw.pos": 30}, "duration": 0.1})
        self.assertEqual(moved["status"], "ok")
        self.assertGreater(moved["duration"], 0.1)

        _, light = self._json("/led/solid", "POST", {"color": [255, 255, 255]})
        self.assertEqual(light["status"], "ok")
        _, color = self._json("/led/color")
        self.assertLessEqual(max(color["color"]), 120)

        with self._response("/camera/snapshot") as response:
            self.assertEqual(response.headers.get_content_type(), "image/jpeg")
            self.assertGreater(len(response.read()), 1_000)

        # 42 is above Lamp's SAFETY.md max_volume, so the route clamps it.
        _, volume = self._json("/audio/volume", "POST", {"volume": 42})
        self.assertEqual(volume, {"status": "ok", "volume": 40, "max_volume": 40})
        _, current_volume = self._json("/audio/volume")
        self.assertEqual(
            current_volume, {"control": "virtual", "volume": 40, "max_volume": 40}
        )
        with self._response("/audio/record?duration_ms=50", "POST") as response:
            with wave.open(BytesIO(response.read())) as captured:
                self.assertEqual(captured.getframerate(), 16_000)
                self.assertEqual(captured.getnchannels(), 1)

        _, sensing = self._json("/sensing")
        self.assertTrue(sensing["running"])
        _, spoken = self._json("/voice/speak", "POST", {"text": "simulation check"})
        self.assertEqual(spoken["status"], "ok")

    def test_mock_replays_recordings_and_led_effects_without_hardware(self):
        _, before = self._json("/servo/position")
        _, played = self._json("/servo/play", "POST", {"recording": "greeting"})
        self.assertEqual(played["status"], "ok")
        time.sleep(0.2)
        _, playback = self._json("/servo")
        self.assertEqual(playback["current"], "greeting")
        _, during = self._json("/servo/position")
        self.assertNotEqual(during["positions"], before["positions"])
        _, stopped = self._json("/servo/stop", "POST", {})
        self.assertEqual(stopped["status"], "ok")
        time.sleep(0.05)
        _, stopped_state = self._json("/servo")
        self.assertIsNone(stopped_state["current"])

        _, effect = self._json(
            "/led/effect", "POST",
            {"effect": "candle", "color": [255, 180, 100], "duration_ms": 500},
        )
        self.assertEqual(effect["effect"], "candle")
        _, stopped_effect = self._json("/led/effect/stop", "POST", {})
        self.assertEqual(stopped_effect["status"], "ok")

    def test_a_suppressed_motor_refuses_play_and_names_the_mode(self):
        """A dropped play is not reported as ok and GET /servo names the dropping mode."""
        try:
            _, held = self._json("/servo/hold", "POST", {})
            self.assertEqual(held["status"], "ok")
            _, state = self._json("/servo")
            self.assertEqual(state["motion_mode"], "hold")

            _, dropped = self._json("/servo/play", "POST", {"recording": "nod"})
            self.assertEqual(dropped["status"], "ignored")
            self.assertEqual(dropped["reason"], "hold")
            time.sleep(0.05)
            _, after = self._json("/servo")
            self.assertNotEqual(after["current"], "nod")

            self._json("/servo/zero", "POST", {})
            _, zeroed = self._json("/servo")
            self.assertEqual(zeroed["motion_mode"], "zero")
            _, dropped_zero = self._json("/servo/play", "POST", {"recording": "nod"})
            self.assertEqual(dropped_zero["reason"], "zero")
        finally:
            self._json("/servo/resume", "POST", {})

        _, resumed = self._json("/servo")
        self.assertIsNone(resumed["motion_mode"])
        _, ok = self._json("/servo/play", "POST", {"recording": "nod"})
        self.assertEqual(ok["status"], "ok")
        self.assertNotIn("reason", ok)
        time.sleep(0.05)
        _, playing = self._json("/servo")
        self.assertEqual(playing["current"], "nod")

    def test_center_is_the_aim_that_recenters_yaw(self):
        """left/right used to be a one-way trip: no aim could bring yaw back."""
        from hal.presets import AIM_CENTER, AIM_PRESETS

        _, left = self._json("/servo/aim", "POST", {"direction": "left", "duration": 0.1})
        self.assertNotAlmostEqual(left["positions"]["base_yaw.pos"], 0.0, places=1)

        _, centered = self._json("/servo/aim", "POST", {"direction": "center", "duration": 0.1})
        self.assertAlmostEqual(
            centered["positions"]["base_yaw.pos"],
            AIM_PRESETS[AIM_CENTER]["base_yaw.pos"],
            places=3,
        )

        self._json("/servo/aim", "POST", {"direction": "left", "duration": 0.1})
        _, up = self._json("/servo/aim", "POST", {"direction": "up", "duration": 0.1})
        self.assertAlmostEqual(
            up["positions"]["base_yaw.pos"],
            AIM_PRESETS["left"]["base_yaw.pos"],
            places=3,
        )

        _, unknown = self._json("/servo/aim", "POST", {"direction": "sideways", "duration": 0.1})
        self.assertAlmostEqual(
            unknown["positions"]["base_yaw.pos"],
            AIM_PRESETS["left"]["base_yaw.pos"],
            places=3,
        )

    def test_named_aims_match_the_reference_lamp_driver(self):
        # Asserted against AIM_PRESETS, never copied numbers, so retuning the table stays green.
        from hal.presets import AIM_PRESETS

        def _aim(direction):
            _, aimed = self._json(
                "/servo/aim", "POST", {"direction": direction, "duration": 0.1}
            )
            return aimed["positions"]

        def _assert_joint(positions, joint, expected, label):
            self.assertAlmostEqual(positions[joint], expected, places=3, msg=label)

        center = AIM_PRESETS["center"]
        centered = _aim("center")
        for joint in ("base_yaw.pos", "base_pitch.pos", "elbow_pitch.pos"):
            _assert_joint(centered, joint, center[joint], f"center {joint}")

        for direction in ("right", "left"):
            aimed = _aim(direction)
            _assert_joint(
                aimed, "base_yaw.pos", AIM_PRESETS[direction]["base_yaw.pos"],
                f"{direction} base_yaw.pos",
            )
            for joint in ("base_pitch.pos", "elbow_pitch.pos"):
                _assert_joint(aimed, joint, center[joint], f"{direction} {joint}")

    def test_local_visualizer_is_available(self):
        with self._response("/simulator") as response:
            page = response.read().decode()
        self.assertIn("Autonomous Lamp", page)
        self.assertIn("/servo/aim", page)
        self.assertIn("/servo/play", page)
        self.assertIn("/led/effect", page)
        self.assertIn("/simulator/cad", page)
        self.assertIn("/simulator/reference", page)
        _, sim_state = self._json("/simulator/state")
        self.assertEqual(sim_state["media"], "virtual")
        from hal.presets import AIM_CENTER, AIM_PRESETS

        self.assertEqual(sim_state["rig_zero"], AIM_PRESETS[AIM_CENTER])
        with self._response("/simulator/reference") as response:
            self.assertEqual(response.headers.get_content_type(), "image/webp")
