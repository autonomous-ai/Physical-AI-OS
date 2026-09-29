"""Tests for the media-owner selector: ROBOT.md `owner:` → handover class."""
import ast
import os
import unittest

from hal.drivers.media_owner.factory import MEDIA_OWNERS, resolve_media_owner

HERE = os.path.dirname(os.path.abspath(__file__))
HAL = os.path.normpath(os.path.join(HERE, ".."))


class TestResolve(unittest.TestCase):
    def test_absent_owner_resolves_to_none(self):
        self.assertIsNone(resolve_media_owner(None))

    def test_unknown_owner_fails_loud(self):
        with self.assertRaises(RuntimeError) as ctx:
            resolve_media_owner("nonexistent_daemon")
        msg = str(ctx.exception)
        self.assertIn("nonexistent_daemon", msg)
        self.assertIn("pollen_daemon", msg)

    def test_pollen_daemon_is_registered(self):
        self.assertIn("pollen_daemon", MEDIA_OWNERS)

    def test_pollen_daemon_resolves(self):
        cls = resolve_media_owner("pollen_daemon")
        self.assertIsNotNone(cls)
        self.assertEqual(cls.__name__, "PollenDaemonMediaOwner")

    def test_registry_entries_are_module_class_pairs(self):
        for name, entry in MEDIA_OWNERS.items():
            self.assertIsInstance(entry, tuple, f"{name} entry must be a tuple")
            self.assertEqual(len(entry), 2, f"{name} entry must be (module, class)")
            module_path, class_name = entry
            self.assertTrue(module_path.startswith("hal."), f"{name}: {module_path}")
            self.assertTrue(class_name and class_name[0].isupper(), f"{name}: {class_name}")


class TestProtocolConformance(unittest.TestCase):
    def test_pollen_implements_media_owner_surface(self):
        """Every registered owner defines the full MediaOwner surface."""
        required = {"release", "acquire"}
        for name, (module_path, class_name) in MEDIA_OWNERS.items():
            path = os.path.join(HAL, *module_path.split(".")[1:]) + ".py"
            self.assertTrue(os.path.isfile(path), f"{name}: no module at {path}")
            tree = ast.parse(open(path).read(), path)
            cls = next(
                (n for n in ast.walk(tree)
                 if isinstance(n, ast.ClassDef) and n.name == class_name),
                None,
            )
            self.assertIsNotNone(cls, f"{name}: {class_name} not found in {path}")
            defined = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
            self.assertTrue(
                required.issubset(defined),
                f"{name}: {class_name} is missing {sorted(required - defined)}",
            )


if __name__ == "__main__":
    unittest.main()
