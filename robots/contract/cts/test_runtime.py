"""Runtime CTS: checks a live device's mounted routes against its ROBOT.md (skipped unless CTS_HAL is set).

Usage: CTS_HAL=http://<device>:5001 [CTS_OS=http://<device>:5000] python3 -m unittest discover -s robots/contract/cts -v
Env: CTS_TIMEOUT, CTS_STOP_BUDGET_MS, CTS_DEVICES_DIR, CTS_ALLOW_MOTION=1 (runs /servo/release, drops a raised arm).
"""
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))  # robots/contract/cts → repo root
sys.path.insert(0, ROOT)  # the hal package lives at the repo root

from hal.board.device import load_device  # noqa: E402  (path set above)

DEVICES_DIR = os.environ.get("CTS_DEVICES_DIR") or os.path.join(ROOT, "robots")

HAL = (os.environ.get("CTS_HAL") or "").rstrip("/")
OS_SERVER = (os.environ.get("CTS_OS") or "").rstrip("/")
TIMEOUT = float(os.environ.get("CTS_TIMEOUT") or 5)
STOP_BUDGET_MS = os.environ.get("CTS_STOP_BUDGET_MS")
ALLOW_MOTION = os.environ.get("CTS_ALLOW_MOTION") == "1"

# Mounted regardless of declaration (hal/server.py `_ALWAYS_ROUTES`).
ALWAYS_ROUTES = {"audio", "emotion", "scene", "system", "bluetooth"}

# Read-only GET probe per route; unlisted routes are not probed.
ROUTE_PROBES = {
    "servo": "/servo",
    "led": "/led",
    "camera": "/camera",
    "audio": "/audio",
    "sensing": "/sensing",
    "environment": "/environment/status",
    "scene": "/scene",
    "display": "/display",
    "emotion": "/emotion/status",
    "voice": "/voice/status",
    "music": "/audio/status",
}


def _request(url, method="GET", timeout=None):
    """Return (http_status, json_or_None, elapsed_ms); never raises, unreachable host -> status 0."""
    req = urllib.request.Request(url, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    except (urllib.error.URLError, OSError):
        return 0, None, (time.perf_counter() - started) * 1000
    elapsed_ms = (time.perf_counter() - started) * 1000
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        body = None
    return status, body, elapsed_ms


@unittest.skipUnless(HAL, "set CTS_HAL=http://<device>:5001 to run the runtime CTS")
class TestRuntimeConformance(unittest.TestCase):
    """Live-device half of the CTS; assertions name the COMPATIBILITY.md rule enforced."""

    @classmethod
    def setUpClass(cls):
        status, body, _ = _request(f"{HAL}/device")
        cls.probe_status = status
        cls.device = body if isinstance(body, dict) else {}
        cls.reachable = status == 200 and isinstance(body, dict)
        cls.mounted = set(cls.device.get("routes") or [])
        cls.profile = None
        dev_id = cls.device.get("id")
        if dev_id and os.path.isfile(os.path.join(DEVICES_DIR, str(dev_id), "ROBOT.md")):
            cls.profile = load_device(str(dev_id), DEVICES_DIR)

    def setUp(self):
        if not self.reachable and self.id().rsplit(".", 1)[-1] != "test_device_is_reachable":
            self.skipTest(f"device at {HAL} not reachable — see test_device_is_reachable")

    def test_device_is_reachable(self):
        """Fails rather than skips: CTS_HAL was set, so a silent pass would be false."""
        self.assertTrue(
            self.reachable,
            f"GET {HAL}/device returned {self.probe_status or 'no response'} — the device is "
            f"unreachable, or is not running an Autonomous HAL. Nothing below could be verified.")

    def _profile(self):
        """Return the repo-side declaration for the device under test, or skip."""
        if self.profile is None:
            self.skipTest(
                f"device reports id={self.device.get('id')!r}, which has no robots/<id>/ROBOT.md "
                f"in this checkout — cannot compare running device against its declaration")
        return self.profile

    def test_must1_identity_is_served(self):
        for field in ("id", "name", "type", "schema", "board"):
            self.assertTrue(self.device.get(field),
                            f"GET /device omits '{field}' — MUST 1 identity is not observable at runtime")
        self.assertEqual(self.device["schema"], "autonomous.device.v1",
                         "MUST 1: running device does not report schema autonomous.device.v1")

    def test_must1_running_board_is_declared(self):
        profile = self._profile()
        board = self.device.get("board")
        self.assertIn(board, profile.boards,
                      f"MUST 1: device booted on board {board!r}, which ROBOT.md does not list "
                      f"({profile.boards}) — the declaration does not describe this hardware")

    def test_must1_reported_id_matches_declaration(self):
        profile = self._profile()
        self.assertEqual(self.device.get("id"), profile.id,
                         "MUST 1: the id the device serves differs from the one its ROBOT.md declares")

    def test_must5_required_routes_are_mounted(self):
        profile = self._profile()
        required = {r for r, req in profile.declared_routes().items() if req}
        missing = sorted(required - self.mounted)
        self.assertFalse(missing,
                         f"MUST 5: {missing} are declared required but are not mounted, yet the device "
                         f"is serving requests — this is the silent half-boot the rule forbids")

    def test_must5_mounted_routes_answer(self):
        """Probe a read-only GET on every mounted route with a known endpoint."""
        unanswered = []
        for route in sorted(self.mounted):
            path = ROUTE_PROBES.get(route)
            if not path:
                continue
            status, _, _ = _request(f"{HAL}{path}")
            if status >= 500:
                unanswered.append(f"{route} ({path} → {status})")
        self.assertFalse(unanswered,
                         f"MUST 5: mounted routes that do not answer: {unanswered}")

    def test_must2_system_capability_is_live(self):
        status, _, _ = _request(f"{HAL}/health")
        self.assertEqual(status, 200,
                         "MUST 2: the 'system' capability must answer — GET /health did not return 200")

    def test_must3_primary_sense_is_mounted(self):
        profile = self._profile()
        groups = set(profile.capabilities)
        primary = {"audio", "vision"} & groups
        self.assertTrue(primary, "MUST 3: device declares no primary sense/output")
        routes = {r for g in primary for r in profile.capabilities[g].routes}
        self.assertTrue(routes & self.mounted,
                        f"MUST 3: declares {sorted(primary)} but mounted none of its routes {sorted(routes)}")

    def test_mustnot16_no_undeclared_route_is_mounted(self):
        profile = self._profile()
        declared = set(profile.declared_routes()) | ALWAYS_ROUTES
        undeclared = sorted(self.mounted - declared)
        self.assertFalse(undeclared,
                         f"MUST NOT 16: {undeclared} are mounted but not declared in ROBOT.md — "
                         f"a skill could reach hardware the device never advertised")

    def test_mustnot15_motion_device_has_a_holding_stop(self):
        """`POST /servo/stop` aborts in-flight motion and holds position."""
        profile = self._profile()
        if "motion" not in profile.capabilities:
            self.skipTest("device declares no motion capability")
        _, before, _ = _request(f"{HAL}/servo/position")
        status, _, elapsed_ms = _request(f"{HAL}/servo/stop", method="POST")
        self.assertLess(status, 400,
                        f"MUST 6: device declares motion but POST /servo/stop returned {status} — "
                        f"no holding stop reachable")
        print(f"\n    [cts] holding stop answered in {elapsed_ms:.0f} ms", end="")
        if STOP_BUDGET_MS:
            self.assertLessEqual(elapsed_ms, float(STOP_BUDGET_MS),
                                 f"MUST 6: stop took {elapsed_ms:.0f} ms, over the "
                                 f"{STOP_BUDGET_MS} ms budget given in CTS_STOP_BUDGET_MS")
        _, after, _ = _request(f"{HAL}/servo/position")
        if before and after:
            for joint, was in (before.get("positions") or {}).items():
                now = (after.get("positions") or {}).get(joint)
                if now is None:
                    continue
                self.assertAlmostEqual(
                    float(was), float(now), delta=2.0,
                    msg=f"MUST 6: /servo/stop moved {joint} from {was} to {now} — "
                        f"a stop must hold, not travel to a rest pose")

    def test_must6_locomotion_body_has_a_holding_stop(self):
        """A `locomotion` body must expose a fast `POST /locomotion/stop`."""
        profile = self._profile()
        if "locomotion" not in (profile.declared_routes() or {}):
            self.skipTest("device declares no locomotion route")
        status, _, elapsed_ms = _request(f"{HAL}/locomotion/stop", method="POST")
        self.assertLess(status, 400,
                        f"MUST 6: device declares the locomotion route but POST /locomotion/stop "
                        f"returned {status} — a rolling body with no stop cannot ship")
        print(f"\n    [cts] locomotion stop answered in {elapsed_ms:.0f} ms", end="")
        if STOP_BUDGET_MS:
            self.assertLessEqual(elapsed_ms, float(STOP_BUDGET_MS),
                                 f"MUST 6: locomotion stop took {elapsed_ms:.0f} ms, over the "
                                 f"{STOP_BUDGET_MS} ms budget given in CTS_STOP_BUDGET_MS")

    def test_mustnot15_tracking_loop_has_a_stop(self):
        profile = self._profile()
        if "motion" not in profile.capabilities:
            self.skipTest("device declares no motion capability")
        status, _, elapsed_ms = _request(f"{HAL}/servo/track/stop", method="POST")
        self.assertLess(status, 400,
                        f"MUST NOT 15: device declares motion but POST /servo/track/stop returned {status} — "
                        f"no deterministic stop reachable")
        print(f"\n    [cts] deterministic stop answered in {elapsed_ms:.0f} ms", end="")
        if STOP_BUDGET_MS:
            self.assertLessEqual(elapsed_ms, float(STOP_BUDGET_MS),
                                 f"MUST 6: stop took {elapsed_ms:.0f} ms, over the "
                                 f"{STOP_BUDGET_MS} ms budget given in CTS_STOP_BUDGET_MS")

    def test_mustnot15_torque_off_is_reachable(self):
        """`/servo/release` (torque off) runs only with CTS_ALLOW_MOTION=1."""
        profile = self._profile()
        if "motion" not in profile.capabilities:
            self.skipTest("device declares no motion capability")
        if not ALLOW_MOTION:
            self.skipTest("set CTS_ALLOW_MOTION=1 to exercise torque-off — it drops a raised arm")
        status, _, elapsed_ms = _request(f"{HAL}/servo/release", method="POST")
        self.assertLess(status, 400,
                        f"MUST NOT 15: POST /servo/release returned {status} — torque-off unreachable")
        print(f"\n    [cts] torque-off answered in {elapsed_ms:.0f} ms", end="")

    @unittest.skipUnless(OS_SERVER, "set CTS_OS=http://<device>:5000 to check the API envelope")
    def test_must7_success_envelope(self):
        status, body, _ = _request(f"{OS_SERVER}/api/health/live")
        self.assertEqual(status, 200, f"GET /api/health/live returned {status}")
        self.assertIsInstance(body, dict, "MUST 7: response body is not a JSON object")
        self.assertEqual(set(body), {"status", "data", "message"},
                         f"MUST 7: envelope keys are {sorted(body)}, expected status/data/message")
        self.assertEqual(body["status"], 1, "MUST 7: a successful call must report status 1")
        self.assertIsNone(body["message"], "MUST 7: message must be null on success")


if __name__ == "__main__":
    unittest.main()
