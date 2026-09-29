"""The mock body: HAL's pieces without hardware under them."""
import importlib.util
import os
import unittest

from hal.board import board
from hal.board.device import load_device, plan_mounts
from hal.safety.policy import min_move_duration, parse_safety

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEVICES_DIR = os.path.join(REPO_ROOT, "robots")


def _load(name, relpath):
    """Import a module by path, avoiding hardware deps in sibling imports."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO_ROOT, relpath))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


base = _load("motion_base", "hal/drivers/motors/base.py")
mock = _load("mock_service", "hal/drivers/motors/mock_service.py")


class TestBoardOverride(unittest.TestCase):
    """HAL_BOARD is what lets HAL boot on a machine with no device tree."""

    def setUp(self):
        self._saved = os.environ.pop(board.BOARD_ENV_VAR, None)

    def tearDown(self):
        os.environ.pop(board.BOARD_ENV_VAR, None)
        if self._saved is not None:
            os.environ[board.BOARD_ENV_VAR] = self._saved

    def test_absent_by_default(self):
        self.assertIsNone(board.board_override())

    def test_forces_a_board_when_set(self):
        os.environ[board.BOARD_ENV_VAR] = "sim"
        self.assertEqual(board.board_override(), "sim")
        self.assertEqual(board.matched_board_id(), "sim")
        self.assertEqual(board.assert_board_supported(["sim"]), "sim")

    def test_refuses_a_board_the_device_does_not_declare(self):
        os.environ[board.BOARD_ENV_VAR] = "sim"
        with self.assertRaises(RuntimeError) as ctx:
            board.assert_board_supported(["raspberry_pi_5"])
        self.assertIn("declares boards", str(ctx.exception))

    def test_refuses_an_invented_board(self):
        os.environ[board.BOARD_ENV_VAR] = "not_a_board"
        with self.assertRaises(RuntimeError) as ctx:
            board.board_override()
        self.assertIn("boards.json", str(ctx.exception))

    def test_detection_is_untouched_when_unset(self):
        self.assertEqual(board.detect_board_id("raspberry pi 5 model b rev 1.0"), "raspberry_pi_5")
        self.assertEqual(board.detect_board_id("some unknown board"), board.DEFAULT_BOARD_ID)

    def test_sim_board_cannot_be_detected_on_real_hardware(self):
        """The sim entry is reachable only through the override."""
        for model in ("raspberry pi 5 model b rev 1.0",
                      "raspberry pi compute module 4 rev 1.1",
                      "orangepi 4 pro sun60iw2"):
            self.assertNotEqual(board.matched_board_id(model), "sim")


class TestSimDeclaration(unittest.TestCase):
    def test_parses_and_declares_only_what_it_has(self):
        profile = load_device("sim", DEVICES_DIR)
        self.assertEqual(set(profile.capabilities), {"motion", "system"})
        self.assertEqual(profile.capabilities["motion"].driver, "mock")
        self.assertTrue(profile.capabilities["motion"].required)

    def test_mounts_the_routes_it_declares_and_no_others(self):
        profile = load_device("sim", DEVICES_DIR)
        declared = profile.declared_routes()
        self.assertEqual(set(declared), {"servo", "system"})
        plan = plan_mounts(declared, {"servo": True, "system": True, "camera": True, "led": True})
        self.assertEqual(set(plan.mounted), {"servo", "system"})
        self.assertNotIn("camera", plan.mounted)
        self.assertNotIn("led", plan.mounted)

    def test_ships_the_bounds_its_motion_requires(self):
        with open(os.path.join(DEVICES_DIR, "sim", "SAFETY.md")) as fh:
            policy = parse_safety(fh.read())
        self.assertEqual(policy.motion.max_speed, 60)
        # 90 degrees in 0.5 s is 180 deg/s; at a 60 deg/s ceiling it takes 1.5 s.
        stretched = min_move_duration(policy, {"base_yaw.pos": 90.0}, {"base_yaw.pos": 0.0}, 0.5)
        self.assertAlmostEqual(stretched, 1.5, places=3)
        self.assertEqual(min_move_duration(policy, {"base_yaw.pos": 10.0}, {"base_yaw.pos": 0.0}, 2.0), 2.0)


class TestMockMotionService(unittest.TestCase):
    def setUp(self):
        self.m = mock.MockMotionService()
        self.m.start()

    def test_satisfies_the_motion_contract(self):
        self.assertIsInstance(self.m, base.MotionService)

    def test_moves_are_recorded_and_readable(self):
        self.m.move_to({"base_yaw.pos": 20.0}, duration=0.5)
        self.assertEqual(self.m.get_positions()["base_yaw.pos"], 20.0)
        self.assertEqual(self.m.calls[-1], ("move_to", {"base_yaw.pos": 20.0}, 0.5))

    def test_aim_and_nudge(self):
        from hal.presets import AIM_CENTER, AIM_PRESETS

        left_yaw = AIM_PRESETS["left"]["base_yaw.pos"]
        self.assertEqual(self.m.aim("left", 0.3, self.m.get_positions(), None)["base_yaw.pos"], left_yaw)
        after = self.m.nudge(-5.0, 10.0, 0.2, self.m.get_positions(), None)
        self.assertEqual(after["base_yaw.pos"], left_yaw - 5.0)
        self.assertEqual(after["base_pitch.pos"], 10.0)
        unknown = self.m.aim("sideways", 0.3, self.m.get_positions(), None)
        self.assertEqual(unknown["base_yaw.pos"], left_yaw - 5.0, "unknown aim moved the yaw")
        self.assertEqual(unknown["elbow_pitch.pos"], AIM_PRESETS[AIM_CENTER]["elbow_pitch.pos"])

    def test_aim_clears_a_previous_halt(self):
        """An aim clears a halt left by a still emotion."""
        from hal.presets import AIM_PRESETS

        self.m.halt()
        moved = self.m.aim("left", 0.3, self.m.get_positions(), None)
        self.assertEqual(moved["base_yaw.pos"], AIM_PRESETS["left"]["base_yaw.pos"],
                         "halt swallowed the aim")

    def test_release_travels_before_torque_off(self):
        """Release reaches rest first, so it is not a stop."""
        self.m.move_to({"base_yaw.pos": 40.0, "base_pitch.pos": 25.0})
        self.assertEqual(self.m.release(), {})
        settled = self.m.get_positions()
        self.assertEqual(settled["base_yaw.pos"], 40.0, "gravity turned the yaw")
        for joint, stop in mock.GRAVITY_REST.items():
            self.assertAlmostEqual(settled[joint], stop, places=3)
        self.assertIn("release", [c[0] for c in self.m.calls])

    def test_halt_holds_position_and_keeps_torque(self):
        """halt() holds position with torque on."""
        self.m.move_to({"base_yaw.pos": 30.0})
        before = self.m.get_positions()
        self.m.halt()
        self.assertEqual(self.m.get_positions(), before, "halt moved the body")
        self.assertTrue(self.m._torque, "halt cut torque — that is release, not stop")

    def test_halt_and_release_are_opposites(self):
        """Same call site, opposite contracts — the confusion #201 is about."""
        self.m.move_to({"base_yaw.pos": 40.0, "base_pitch.pos": 25.0})
        parked = self.m.get_positions()["base_yaw.pos"]
        parked_pitch = self.m.get_positions()["base_pitch.pos"]
        self.m.halt()
        self.assertEqual(self.m.get_positions()["base_yaw.pos"], parked)
        self.assertTrue(self.m._torque)
        self.m.release()
        self.assertLess(
            self.m.get_positions()["base_pitch.pos"], parked_pitch,
            "release left the arm holding its pitch — that is a halt",
        )
        self.assertFalse(self.m._torque)

    def test_halt_is_idempotent_and_cleared_by_the_next_move(self):
        """A halt survives its interrupted move; the next move clears it."""
        self.m.halt()
        self.m.halt()
        self.assertTrue(self.m._halted)
        self.m.move_to({"base_yaw.pos": 5.0})
        self.assertFalse(self.m._halted)

    def test_unknown_joints_are_ignored(self):
        self.m.send_positions({"nonexistent.pos": 5.0})
        self.assertNotIn("nonexistent.pos", self.m.get_positions())


if __name__ == "__main__":
    unittest.main()
