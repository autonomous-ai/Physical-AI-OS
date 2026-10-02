"""MPR121 device declarations and legacy-input fallback without hardware."""

import json
from pathlib import Path
import tempfile
import unittest

from hal.board.mpr121 import MPR121Config, load_mpr121_config


class TestMPR121Config(unittest.TestCase):
    def test_retired_chord_config_does_not_block_component_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mpr121.json"
            path.write_text(json.dumps({"boards": {"orangepi_sun60": {
                "bus": 0, "swipe_axis": [0, 1], "harness_voice_chord": [0, 11],
            }}}))
            config = load_mpr121_config(directory, "orangepi_sun60")
            self.assertEqual(config.swipe_axis, (0, 1))
            self.assertFalse(hasattr(config, "harness_voice_chord"))

    def test_only_lamp_declares_hardware(self):
        root = Path(__file__).resolve().parents[2] / "robots"
        config = load_mpr121_config(root / "lamp", "orangepi_sun60")
        self.assertIsInstance(config, MPR121Config)
        self.assertEqual(config.tap_min_electrodes, 3)
        self.assertIsNone(load_mpr121_config(root / "intern-v2", "orangepi_sun60"))
        self.assertIsNone(load_mpr121_config(root / "lamp", "raspberry_pi_5"))

    def test_defaults_match_supplied_script(self):
        config = MPR121Config(bus=5)
        self.assertEqual(config.address, 0x5A)
        self.assertEqual(config.tap_min_electrodes, 1)
        self.assertEqual((config.touch_threshold, config.release_threshold), (2, 1))
        self.assertEqual(config.electrodes, tuple(range(12)))

    def test_device_selection_and_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(load_mpr121_config(directory, "orangepi_sun60"))
            path = Path(directory) / "mpr121.json"
            path.write_text(json.dumps({"boards": {
                "orangepi_sun60": {"bus": 5, "address": 91, "electrodes": [2, 4]},
                "raspberry_pi_4": {"enabled": False},
            }}))
            self.assertEqual(load_mpr121_config(directory, "orangepi_sun60"),
                             MPR121Config(bus=5, address=91, electrodes=(2, 4)))
            self.assertIsNone(load_mpr121_config(directory, "raspberry_pi_4"))
            self.assertIsNone(load_mpr121_config(directory, "sim"))

    def test_invalid_values(self):
        for overrides in (
            {"bus": -1}, {"bus": True}, {"address": 0x40},
            {"electrodes": []}, {"electrodes": [12]}, {"electrodes": [1, 1]},
            {"electrodes": [True]}, {"touch_threshold": 256},
            {"release_threshold": 2}, {"autoconfig": "true"},
            {"poll_ms": 0}, {"debounce_ms": -1},
            {"tap_min_electrodes": 0}, {"tap_min_electrodes": True},
            {"tap_min_electrodes": 13}, {"tap_min_electrodes": 1.5},
            {"electrodes": [0, 1], "tap_min_electrodes": 3},
            {"ffi": 7}, {"sfi": 8}, {"esi_ms": 3}, {"ffi": True},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                MPR121Config(**({"bus": 5} | overrides))

    def test_invalid_file_never_silently_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mpr121.json"
            for value in ("{", "null", '{"boards": []}',
                          '{"boards": {"orangepi_sun60": {}}}',
                          '{"boards": {"orangepi_sun60": {"bus": 5, "adress": 90}}}'):
                with self.subTest(value=value):
                    path.write_text(value)
                    with self.assertRaisesRegex(ValueError, "mpr121.json"):
                        load_mpr121_config(directory, "orangepi_sun60")

    def test_swipe_axis_json_round_trip_and_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mpr121.json"
            path.write_text(json.dumps({"boards": {"orangepi_sun60": {
                "bus": 0, "electrodes": [2, 4, 6], "swipe_axis": [6, 4, 2],
            }}}))
            self.assertEqual(load_mpr121_config(directory, "orangepi_sun60").swipe_axis, (6, 4, 2))
            path.write_text(json.dumps({"boards": {"orangepi_sun60": {
                "bus": 0, "electrodes": [2, 4], "swipe_axis": [2, 6],
            }}}))
            with self.assertRaisesRegex(ValueError, "swipe_axis"):
                load_mpr121_config(directory, "orangepi_sun60")
