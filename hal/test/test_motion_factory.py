"""Tests for the motion driver factory (hal/drivers/motors/factory.py)."""
import ast
import os
import unittest

from hal.board.device import extract_front_matter, parse_capabilities
from hal.drivers.motors.factory import MOTION_DRIVERS, resolve_motion_class

HERE = os.path.dirname(os.path.abspath(__file__))
DEVICES_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "robots"))


class TestResolveMotionClass(unittest.TestCase):
    def test_feetech_resolves_to_animation_service(self):
        cls = resolve_motion_class("feetech", required=True)
        self.assertIsNotNone(cls)
        self.assertEqual(cls.__name__, "AnimationService")

    def test_absent_driver_defaults_to_feetech(self):
        default_cls = resolve_motion_class(None, required=True)
        feetech_cls = resolve_motion_class("feetech", required=True)
        self.assertIs(default_cls, feetech_cls)

    def test_unknown_driver_required_fails_loud(self):
        with self.assertRaises(RuntimeError) as ctx:
            resolve_motion_class("no_such_driver", required=True)
        msg = str(ctx.exception)
        self.assertIn("no_such_driver", msg)
        self.assertIn("feetech", msg)

    def test_unknown_driver_optional_returns_none(self):
        self.assertIsNone(resolve_motion_class("no_such_driver", required=False))

    def test_registry_entries_are_module_class_pairs(self):
        for driver, entry in MOTION_DRIVERS.items():
            self.assertEqual(len(entry), 2, f"{driver}: expected (module, class)")

    def test_stackchan_resolves_to_wifi_motion_service(self):
        cls = resolve_motion_class("stackchan", required=True)
        self.assertIsNotNone(cls)
        self.assertEqual(cls.__name__, "StackChanMotionService")


class TestReachyMiniDeclaration(unittest.TestCase):
    """Guard the committed reachy-mini ROBOT.md: motion declares reachy_sdk."""

    def _load_caps(self):
        path = os.path.join(DEVICES_DIR, "reachy-mini", "ROBOT.md")
        with open(path, encoding="utf-8") as f:
            return parse_capabilities(extract_front_matter(f.read()))

    def test_motion_driver_is_reachy_sdk(self):
        caps = self._load_caps()
        self.assertIn("motion", caps)
        self.assertEqual(caps["motion"].driver, "reachy_sdk")
        self.assertTrue(caps["motion"].required)

    def test_reachy_sdk_is_registered(self):
        self.assertIn("reachy_sdk", MOTION_DRIVERS)
        module_path, class_name = MOTION_DRIVERS["reachy_sdk"]
        self.assertEqual(class_name, "ReachyMotionService")

    def test_reachy_sdk_resolves_or_degrades(self):
        # reachy-mini is absent on dev machines -> None; on-device it resolves. Never raises.
        cls = resolve_motion_class("reachy_sdk", required=True)
        if cls is not None:
            self.assertEqual(cls.__name__, "ReachyMotionService")


_PROTOCOL_MEMBERS = {
    "start", "stop", "is_connected",
    "dispatch", "get_available_recordings", "add_recording", "ensure_running",
    "is_suppressed", "motion_mode",
    "freeze", "unfreeze", "is_frozen",
    "move_to", "move_and_hold", "get_joint_names", "get_positions",
    "send_positions",
    "zero_pose", "release", "resume", "hold", "joint_status",
    "aim", "nudge",
}


class TestReachyServiceConformance(unittest.TestCase):
    """AST-level protocol conformance — no reachy_mini import needed."""

    def test_reachy_service_implements_protocol_surface(self):
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "drivers", "motors", "reachy_service.py",
        )
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())

        cls = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.ClassDef) and n.name == "ReachyMotionService"),
            None,
        )
        self.assertIsNotNone(cls, "ReachyMotionService class not found")

        defined = {
            n.name for n in cls.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        missing = _PROTOCOL_MEMBERS - defined
        self.assertFalse(
            missing, f"ReachyMotionService missing protocol members: {sorted(missing)}"
        )


class TestReachyMoveMap(unittest.TestCase):
    """Verify _MOVE_MAP covers every SERVO_* preset and targets valid HF moves."""

    _HF_MOVES = {
        "amazed1", "anxiety1", "attentive1", "attentive2", "boredom1", "boredom2",
        "calming1", "cheerful1", "come1", "confused1", "contempt1", "curious1",
        "dance1", "dance2", "dance3", "disgusted1", "displeased1", "displeased2",
        "downcast1", "dying1", "electric1", "enthusiastic1", "enthusiastic2",
        "exhausted1", "fear1", "frustrated1", "furious1", "go_away1", "grateful1",
        "helpful1", "helpful2", "impatient1", "impatient2", "incomprehensible2",
        "indifferent1", "inquiring1", "inquiring2", "inquiring3", "irritated1",
        "irritated2", "laughing1", "laughing2", "lonely1", "lost1", "loving1",
        "no1", "no_excited1", "no_sad1", "oops1", "oops2", "proud1", "proud2",
        "proud3", "rage1", "relief1", "relief2", "reprimand1", "reprimand2",
        "reprimand3", "resigned1", "sad1", "sad2", "scared1", "serenity1", "shy1",
        "sleep1", "success1", "success2", "surprised1", "surprised2", "thoughtful1",
        "thoughtful2", "tired1", "uncertain1", "uncomfortable1", "understanding1",
        "understanding2", "welcoming1", "welcoming2", "yes1", "yes_sad1",
    }

    def _get_move_map(self):
        """Load _MOVE_MAP via AST to avoid importing reachy_mini SDK."""
        import importlib
        presets = importlib.import_module("hal.presets")
        path = os.path.join(HERE, "..", "drivers", "motors", "reachy_service.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        for node in ast.iter_child_nodes(tree):
            # _MOVE_MAP uses a type annotation → AnnAssign, not Assign.
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "_MOVE_MAP" and node.value:
                    code = compile(ast.Expression(body=node.value), path, "eval")
                    return eval(code, {"P": presets})
        self.fail("_MOVE_MAP not found in reachy_service.py")

    def test_all_servo_presets_mapped(self):
        """Every SERVO_* recording constant in presets.py has a _MOVE_MAP entry."""
        import hal.presets as P
        move_map = self._get_move_map()
        servo_names = {
            v for k, v in vars(P).items()
            if k.startswith("SERVO_") and not k.startswith("SERVO_CMD_")
            and isinstance(v, str)
        }
        unmapped = servo_names - set(move_map.keys())
        # Feetech-only recordings with no Reachy equivalent may stay unmapped.
        acceptable_unmapped = {"tracking", "test"}
        real_missing = unmapped - acceptable_unmapped
        self.assertFalse(
            real_missing,
            f"SERVO_* presets missing from _MOVE_MAP: {sorted(real_missing)}"
        )

    def test_all_map_values_are_valid_hf_moves(self):
        """Every _MOVE_MAP value must be a real HF move name."""
        move_map = self._get_move_map()
        invalid = {v for v in move_map.values() if v not in self._HF_MOVES}
        self.assertFalse(
            invalid,
            f"_MOVE_MAP targets not in HF emotions library: {sorted(invalid)}"
        )

    def test_map_is_not_empty(self):
        move_map = self._get_move_map()
        self.assertGreaterEqual(len(move_map), 20)


if __name__ == "__main__":
    unittest.main()


class TestShutdownTorquePolicy(unittest.TestCase):
    """The shutdown path calls MotionService.stop(); that must not go limp."""

    def test_feetech_config_keeps_torque_on_disconnect(self):
        import ast
        import os

        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "drivers", "motors", "animation_service.py",
        )
        tree = ast.parse(open(path).read(), path)
        found = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "LeLampFollowerConfig"
        ]
        self.assertEqual(len(found), 1, "expected one LeLampFollowerConfig(...)")
        kwargs = {k.arg: k.value for k in found[0].keywords}
        self.assertIn(
            "disable_torque_on_disconnect", kwargs,
            "must be explicit — the lerobot default cuts torque on every restart",
        )
        self.assertFalse(
            kwargs["disable_torque_on_disconnect"].value,
            "shutdown must keep torque; going limp is release()'s job",
        )
