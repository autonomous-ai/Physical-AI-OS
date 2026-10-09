"""Device-specific servo overload thresholds, read without hardware."""

import json
from pathlib import Path
import tempfile
import unittest

from hal.board.servo_overload import (
    ContactStopConfig,
    ServoOverloadConfig,
    load_servo_overload_config,
)


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
        self.assertEqual(
            config,
            ServoOverloadConfig(
                800, 1.0, 120.0,
                ContactStopConfig({"base_yaw": 650, "wrist_roll": 750, "wrist_pitch": 650},
                                  0.05, 3.0, 80, 25, {
                                      "base_pitch": 560, "elbow_pitch": 400,
                                      "lag:base_yaw": 70, "lag:base_pitch": 100,
                                      "lag:elbow_pitch": 70, "lag:wrist_roll": 40,
                                      "lag:wrist_pitch": 90,
                                  }),
                {"base_pitch": 700, "elbow_pitch": 700},
            ),
        )
        self.assertIsNone(load_servo_overload_config(root / "intern-v2", "orangepi_sun60"))

    def test_malformed_configuration_is_rejected(self):
        entries = [
            {"load": 0, "hold_s": 1, "retry_s": 120}, {"load": 1001, "hold_s": 1, "retry_s": 120},
            {"load": 80.0, "hold_s": 1, "retry_s": 120}, {"load": True, "hold_s": 1, "retry_s": 120},
            {"load": 800, "hold_s": 0, "retry_s": 120}, {"load": 800, "hold_s": 1, "retry_s": -1},
            {"load": 800, "hold_s": "1", "retry_s": 120}, {"load": 800, "hold_s": 1},
            {"load": 800, "hold_s": 1, "retry_s": 120, "chip": 0}, {"enabled": "false"},
            {"load": 800, "hold_s": 1, "retry_s": 120, "contact": []},
            {"load": 800, "hold_s": 1, "retry_s": 120, "contact": {"load": 950, "hold_s": 0.1}},
            {"load": 800, "hold_s": 1, "retry_s": 120,
             "contact": {"load": 1001, "hold_s": 0.1, "pause_s": 3}},
            {"load": 800, "hold_s": 1, "retry_s": 120,
             "contact": {"load": 950, "hold_s": 0, "pause_s": 3}},
            {"load": 800, "hold_s": 1, "retry_s": 120,
             "contact": {"load": {}, "hold_s": 0.1, "pause_s": 3}},
            {"load": 800, "hold_s": 1, "retry_s": 120,
             "contact": {"load": {"base_yaw": 0}, "hold_s": 0.1, "pause_s": 3}},
            {"load": 800, "hold_s": 1, "retry_s": 120,
             "contact": {"load": {"base_yaw": 65.0}, "hold_s": 0.1, "pause_s": 3}},
            {"load": {"base_yaw": 800}, "hold_s": 1, "retry_s": 120},
            {"load": 800, "hold_s": 1, "retry_s": 120, "torque_limit": {}},
            {"load": 800, "hold_s": 1, "retry_s": 120,
             "contact": {"load": 950, "hold_s": 0.1, "pause_s": 3, "profile_margin": 0}},
            {"load": 800, "hold_s": 1, "retry_s": 120,
             "contact": {"load": 950, "hold_s": 0.1, "pause_s": 3, "margin": 150}},
            {"load": 800, "hold_s": 1, "retry_s": 120,
             "contact": {"load": 950, "hold_s": 0.1, "pause_s": 3, "lag_margin": 40}},
            {"load": 800, "hold_s": 1, "retry_s": 120,
             "contact": {"load": 950, "hold_s": 0.1, "pause_s": 3, "off_playback": {}}},
            {"load": 800, "hold_s": 1, "retry_s": 120,
             "contact": {"load": 950, "hold_s": 0.1, "pause_s": 3,
                         "off_playback": {"lag:base_pitch": 0}}},
            {"load": 800, "hold_s": 1, "retry_s": 120, "torque_limit": 700},
            {"load": 800, "hold_s": 1, "retry_s": 120, "torque_limit": {"base_pitch": 0}},
            {"load": 800, "hold_s": 1, "retry_s": 120, "torque_limit": {"base_pitch": 70.0}},
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
