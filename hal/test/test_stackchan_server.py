"""Real HAL startup and HTTP-to-WebSocket proof without robot hardware."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from dotenv import dotenv_values
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "stackchan-startup-test-token-32-characters"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestStackChanServer(unittest.TestCase):
    def test_real_driver_boots_and_controls_loopback_firmware(self):
        for bench in (False, True):
            with self.subTest(bench=bench):
                self._exercise_server(bench)

    def _exercise_server(self, bench):
        with tempfile.TemporaryDirectory(prefix="stackchan-hal-") as directory:
            http_port, body_port = free_port(), free_port()
            while body_port == http_port:
                body_port = free_port()
            env = os.environ.copy()
            env.update({
                "PYTHONPATH": str(REPO_ROOT),
                "DEVICES_DIR": str(REPO_ROOT / "robots/_experimental"),
                "HAL_MODE": "developer",
                "HAL_LOG_DIR": directory, "HAL_STATE_DIR": directory,
                "HAL_USERS_DIR": directory + "/users",
                "HAL_STRANGERS_DIR": directory + "/strangers",
                "OS_CONFIG_PATH": directory + "/missing-config.json",
            })
            # Exercise the documented profile env-file path, not an unrelated
            # set of hand-built process variables. Only bench connection values
            # change; board, device and transport defaults come from the profile.
            profile_env = dotenv_values(
                REPO_ROOT / "robots/_experimental/stackchan/rootfs/opt/hal/.env"
            )
            for key in profile_env:
                env.pop(key, None)
            profile_env.update({
                "STACKCHAN_DEVICE_ID": "stackchan-test",
                "STACKCHAN_BODY_TOKEN": TOKEN,
                "STACKCHAN_BODY_HOST": "127.0.0.1",
                "STACKCHAN_BODY_PORT": str(body_port),
                "STACKCHAN_BODY_ALLOW_INSECURE_WS": "1",
                "STACKCHAN_BODY_TLS_CERT": "", "STACKCHAN_BODY_TLS_KEY": "",
            })
            env_file = Path(directory) / ".env"
            env_file.write_text("".join(f"{key}={value}\n" for key, value in profile_env.items()))

            def request(path, body=None):
                data = json.dumps(body).encode() if body is not None else None
                req = urllib.request.Request(
                    f"http://127.0.0.1:{http_port}{path}", data=data,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as response:
                    return json.load(response)

            with open(directory + "/server.log", "w+") as log:
                entrypoint = (["robots._experimental.stackchan.commissioning:create_app", "--factory"]
                              if bench else ["hal.server:app"])
                server = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", *entrypoint, "--host",
                     "127.0.0.1", "--port", str(http_port), "--env-file", str(env_file)],
                    cwd=REPO_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                )
                try:
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        if server.poll() is not None:
                            log.seek(0)
                            self.fail("HAL exited during startup:\n" + log.read())
                        try:
                            device = request("/device")
                            break
                        except urllib.error.URLError:
                            time.sleep(0.1)
                    else:
                        log.seek(0)
                        self.fail("HAL startup timed out:\n" + log.read())
                    self.assertEqual(device["id"], "stackchan")
                    self.assertEqual(device["board"], "host")
                    self.assertEqual(device["drivers"]["motion"], "stackchan")
                    health = request("/health")
                    self.assertFalse(health["servo"])
                    self.assertFalse(health["audio"])
                    self.assertFalse(health["camera"])
                    self.assertFalse(health["led"])

                    routes = request("/openapi.json")["paths"]
                    bench_paths = {"/stackchan/home", "/stackchan/home/position", "/stackchan/home/move",
                                   "/stackchan/stop", "/stackchan/release"}
                    self.assertEqual(bench_paths.intersection(routes), bench_paths if bench else set())
                    self.assertFalse(any(path.startswith("/servo/home") for path in routes))
                    if bench:
                        home = request("/stackchan/home")
                        self.assertFalse(home["connected"])
                        self.assertFalse(home["motion_enabled"])
                    else:
                        for path in bench_paths | {"/servo/home", "/servo/home/position", "/servo/home/move"}:
                            with self.assertRaises(urllib.error.HTTPError) as caught:
                                request(path, {} if path.endswith(("/move", "/stop", "/release")) else None)
                            self.assertEqual(caught.exception.code, 404)

                    with connect(
                        f"ws://127.0.0.1:{body_port}/stackchan/body/v1",
                        additional_headers={"Authorization": f"Bearer {TOKEN}"},
                    ) as ws:
                        ws.send(json.dumps({
                            "v": 1, "type": "hello", "device_id": "stackchan-test",
                            "capabilities": ["motion.pan_tilt", "motion.measured_position",
                                             "motion.halt_hold", "motion.torque_release",
                                             "motion.timed_move", "motion.home_degrees.v1"],
                        }))
                        self.assertEqual(json.loads(ws.recv(timeout=2))["type"], "hello.accepted")
                        # Starting the host and authenticating must not move the body.
                        with self.assertRaises(TimeoutError):
                            ws.recv(timeout=0.1)
                        commands, peer_errors = [], []
                        positions = {"pan": 0.0, "tilt": 0.0}

                        def firmware():
                            try:
                                for raw in ws:
                                    message = json.loads(raw)
                                    commands.append(message)
                                    op = message["op"]
                                    result = {}
                                    if op == "motion.get":
                                        result = {"positions": dict(positions)}
                                    elif op == "motion.move":
                                        native = message["args"]["motion"]
                                        if "yawServo" in native:
                                            positions["pan"] = native["yawServo"]["angle"] / 10
                                        result = {"state": "scheduled"}
                                    ws.send(json.dumps({"v": 1, "id": message["id"],
                                                        "status": "completed", "result": result}))
                            except ConnectionClosed:
                                pass
                            except Exception as exc:
                                peer_errors.append(exc)

                        peer = threading.Thread(target=firmware, daemon=True)
                        peer.start()
                        try:
                            self.assertTrue(request("/health")["servo"])
                            if bench:
                                home = request("/stackchan/home")
                                self.assertTrue(home["connected"])
                                self.assertTrue(home["supported"])
                                self.assertEqual(commands, [])
                            moved = request("/servo/move", {
                                "positions": {"base_yaw.pos": 10}, "duration": 0.05,
                            })
                            self.assertEqual(moved["status"], "ok")
                            self.assertEqual(moved["duration"], 1.0)
                            move = next(m for m in commands if m["op"] == "motion.move")
                            self.assertEqual(move["args"]["duration_ms"], 1000)
                            self.assertEqual(request("/stackchan/stop" if bench else "/servo/stop", {})["status"], "ok")
                            self.assertIn("motion.halt", [m["op"] for m in commands])
                        finally:
                            ws.close()
                            peer.join(timeout=3)
                        self.assertFalse(peer.is_alive())
                        self.assertEqual(peer_errors, [])
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=5)
