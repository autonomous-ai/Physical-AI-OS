"""Tests for the per-device preset overlay (board/presets_overlay.py)."""
import copy
import json
import os
import tempfile
import unittest

from hal import presets
from hal.board.presets_overlay import (
    DEFAULT_LED_COUNT,
    _merge_table,
    apply_device_presets,
)


class TestMergeTable(unittest.TestCase):
    def test_patches_only_named_fields(self):
        base = {"listening": {"color": [51, 121, 230], "effect": "pulse", "speed": 1.5}}
        _merge_table("emotion", base, {"listening": {"color": [255, 120, 0]}}, "demo")
        self.assertEqual(base["listening"], {"color": [255, 120, 0], "effect": "pulse", "speed": 1.5})

    def test_leaves_other_entries_untouched(self):
        base = {"listening": {"color": [1, 1, 1]}, "happy": {"color": [2, 2, 2]}}
        _merge_table("emotion", base, {"listening": {"color": [9, 9, 9]}}, "demo")
        self.assertEqual(base["happy"], {"color": [2, 2, 2]})

    def test_unknown_preset_key_fails_loud(self):
        base = {"listening": {"color": [1, 1, 1]}}
        with self.assertRaises(ValueError) as cm:
            _merge_table("emotion", base, {"listenign": {"color": [9, 9, 9]}}, "demo")
        self.assertIn("listenign", str(cm.exception))

    def test_non_dict_section_fails(self):
        with self.assertRaises(ValueError):
            _merge_table("emotion", {}, ["not", "a", "dict"], "demo")

    def test_non_dict_entry_fails(self):
        with self.assertRaises(ValueError):
            _merge_table("emotion", {"listening": {}}, {"listening": [1, 2, 3]}, "demo")


class TestApplyDevicePresets(unittest.TestCase):
    def setUp(self):
        # Snapshot and restore the real module tables so mutation never leaks across tests.
        self._emotion = copy.deepcopy(presets.EMOTION_PRESETS)
        self._scene = copy.deepcopy(presets.SCENE_PRESETS)
        self._aim = copy.deepcopy(presets.AIM_PRESETS)
        self._status = copy.deepcopy(presets.STATUS_LED_PRESETS)
        self._ambient = copy.deepcopy(presets.AMBIENT_RESTING_LED)

    def tearDown(self):
        presets.AMBIENT_RESTING_LED.clear()
        presets.AMBIENT_RESTING_LED.update(self._ambient)
        presets.EMOTION_PRESETS.clear()
        presets.EMOTION_PRESETS.update(self._emotion)
        presets.SCENE_PRESETS.clear()
        presets.SCENE_PRESETS.update(self._scene)
        presets.AIM_PRESETS.clear()
        presets.AIM_PRESETS.update(self._aim)
        presets.STATUS_LED_PRESETS.clear()
        presets.STATUS_LED_PRESETS.update(self._status)

    def _write(self, tmp, device_type, payload):
        d = os.path.join(tmp, device_type)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "presets.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def test_device_resting_presets_update_existing_reference(self):
        reference = presets.AMBIENT_RESTING_LED
        root = os.path.join(os.path.dirname(__file__), "..", "..", "robots")
        for device_type in ("lamp", "intern-v2"):
            with self.subTest(device_type=device_type):
                with open(os.path.join(root, device_type, "presets.json"), encoding="utf-8") as f:
                    expected = json.load(f)["ambient_led"]["resting"]
                apply_device_presets(device_type, root)
                self.assertIs(reference, presets.AMBIENT_RESTING_LED)
                self.assertEqual(reference, expected)
                self.assertEqual(presets.ambient_resting_is_dark(), not any(expected["color"]))

    def test_no_file_keeps_base_and_default_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            count = apply_device_presets("ghost", tmp)
        self.assertEqual(count, DEFAULT_LED_COUNT)
        self.assertEqual(presets.EMOTION_PRESETS["listening"]["color"], self._emotion["listening"]["color"])

    def test_overrides_color_and_led_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "demo", {
                "led_count": 60,
                "emotion": {"listening": {"color": [255, 120, 0]}},
            })
            count = apply_device_presets("demo", tmp)
        self.assertEqual(count, 60)
        self.assertEqual(presets.EMOTION_PRESETS["listening"]["color"], [255, 120, 0])
        self.assertEqual(presets.EMOTION_PRESETS["listening"]["effect"],
                         self._emotion["listening"]["effect"])

    def test_overrides_status_led(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "demo", {"status_led": {"booting": {"color": [10, 20, 30]}}})
            apply_device_presets("demo", tmp)
        self.assertEqual(presets.STATUS_LED_PRESETS["booting"]["color"], [10, 20, 30])
        self.assertEqual(presets.STATUS_LED_PRESETS["booting"]["effect"],
                         self._status["booting"]["effect"])

    def test_overrides_scene_and_aim(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "demo", {
                "scene": {"relax": {"brightness": 0.3}},
                "aim": {"desk": {"base_pitch.pos": 8.0}},
            })
            apply_device_presets("demo", tmp)
        self.assertEqual(presets.SCENE_PRESETS["relax"]["brightness"], 0.3)
        self.assertEqual(presets.AIM_PRESETS["desk"]["base_pitch.pos"], 8.0)

    def test_unknown_emotion_fails_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "demo", {"emotion": {"nope": {"color": [1, 1, 1]}}})
            with self.assertRaises(ValueError):
                apply_device_presets("demo", tmp)

    def test_bad_led_count_fails(self):
        for bad in (0, -5, True, "64", 1.5):
            with self.subTest(bad=bad):
                with tempfile.TemporaryDirectory() as tmp:
                    self._write(tmp, "demo", {"led_count": bad})
                    with self.assertRaises(ValueError):
                        apply_device_presets("demo", tmp)

    def test_malformed_json_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "demo")
            os.makedirs(d)
            with open(os.path.join(d, "presets.json"), "w", encoding="utf-8") as f:
                f.write("{not json")
            with self.assertRaises(json.JSONDecodeError):
                apply_device_presets("demo", tmp)

    def test_ignores_comment_and_unknown_top_level_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "demo", {
                "_comment": "this is a doc string, not a section",
                "future_field": 123,
                "emotion": {"listening": {"color": [1, 2, 3]}},
            })
            count = apply_device_presets("demo", tmp)
        self.assertEqual(count, DEFAULT_LED_COUNT)
        self.assertEqual(presets.EMOTION_PRESETS["listening"]["color"], [1, 2, 3])

    def test_shipped_example_file_is_valid(self):
        here = os.path.dirname(os.path.abspath(__file__))
        example = os.path.normpath(
            os.path.join(here, "..", "..", "robots", "_base", "presets.example.json")
        )
        with open(example, "r", encoding="utf-8") as f:
            payload = json.load(f)
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "demo", payload)
            count = apply_device_presets("demo", tmp)
        self.assertIsInstance(count, int)


class TestStatusLedPresetKeys(unittest.TestCase):
    def test_keys_match_go_status_states(self):
        # Must equal every status name the Go side POSTs to HAL /led/status.
        expected = {
            "ota", "error", "booting", "connectivity", "wifi_connecting",
            "hal_down", "agent_down", "hardware", "ready_flash",
            "ota_progress", "ota_error", "ota_success", "setup",
            "mic_muted",
        }
        self.assertEqual(set(presets.STATUS_LED_PRESETS), expected)
        for state, p in presets.STATUS_LED_PRESETS.items():
            self.assertTrue(p["effect"] == "solid" or p["effect"] in presets.VALID_LED_EFFECTS, state)
            self.assertEqual(len(p["color"]), 3, state)
            self.assertIn("speed", p)


if __name__ == "__main__":
    unittest.main()
