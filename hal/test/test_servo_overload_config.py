"""Device-specific servo overload thresholds, read without hardware."""

import json
from pathlib import Path
import tempfile
import unittest

from hal.board.servo_overload import ServoOverloadConfig, load_servo_overload_config


class TestServoOverloadConfig(unittest.TestCase):
    def test_missing_file_or_board_means_no_cut_off(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(load_servo_overload_config(directory, "orangepi_sun60"))
            (Path(directory) / "servo_overload.json").write_text('{"boards": {}}')
            self.assertIsNone(load_servo_overload_config(directory, "orangepi_sun60"))

    def test_board_entry_and_explicit_disable(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "servo_overload.json").write_text(json.dumps({"boards": {
                "orangepi_sun60": {"load": 700, "hold_s": 2, "retry_s": 60.5},
                "raspberry_pi_5": {"enabled": False},
            }}))
            config = load_servo_overload_config(directory, "orangepi_sun60")
            self.assertEqual(config, ServoOverloadConfig(700, 2.0, 60.5))
            self.assertIsInstance(config.hold_s, float)
            self.assertIsNone(load_servo_overload_config(directory, "raspberry_pi_5"))
            self.assertIsNone(load_servo_overload_config(directory, "sim"))

    def test_lamp_declares_the_cut_off(self):
        root = Path(__file__).resolve().parents[2] / "robots"
        config = load_servo_overload_config(root / "lamp", "orangepi_sun60")
        self.assertEqual(config, ServoOverloadConfig(800, 1.0, 120.0))
        self.assertIsNone(load_servo_overload_config(root / "intern-v2", "orangepi_sun60"))

    def test_malformed_configuration_is_rejected(self):
        entries = [
            {"load": 0, "hold_s": 1, "retry_s": 120}, {"load": 1001, "hold_s": 1, "retry_s": 120},
            {"load": 80.0, "hold_s": 1, "retry_s": 120}, {"load": True, "hold_s": 1, "retry_s": 120},
            {"load": 800, "hold_s": 0, "retry_s": 120}, {"load": 800, "hold_s": 1, "retry_s": -1},
            {"load": 800, "hold_s": "1", "retry_s": 120}, {"load": 800, "hold_s": 1},
            {"load": 800, "hold_s": 1, "retry_s": 120, "chip": 0}, {"enabled": "false"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "servo_overload.json"
            values = ["{", "null", '{"boards": []}', '{"boards": {}, "x": 1}'] + [
                json.dumps({"boards": {"orangepi_sun60": entry}}) for entry in entries
            ]
            for value in values:
                with self.subTest(value=value):
                    path.write_text(value)
                    with self.assertRaisesRegex(ValueError, "servo_overload.json"):
                        load_servo_overload_config(directory, "orangepi_sun60")
