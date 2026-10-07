"""Device-specific touch wiring selection without GPIO hardware."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from hal.board.board import TouchConfig
from hal.board.ttp223 import load_touch_config


class TestTouchConfig(unittest.TestCase):
    def test_missing_file_and_board_preserve_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_touch_config(directory, "orangepi_sun60"),
                             TouchConfig(0, [96, 100]))
            self.assertIsNone(load_touch_config(directory, "raspberry_pi_5"))
            (Path(directory) / "ttp223.json").write_text('{"boards": {}}')
            self.assertEqual(load_touch_config(directory, "orangepi_sun60"),
                             TouchConfig(0, [96, 100]))

    def test_device_override_axis_and_explicit_disable(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "ttp223.json").write_text(json.dumps({"boards": {
                "orangepi_sun60": {"chip": 2, "lines": [3, 7], "axis": [7, 3]},
                "raspberry_pi_5": {"enabled": False},
            }}))
            self.assertEqual(load_touch_config(directory, "orangepi_sun60"),
                             TouchConfig(2, [3, 7], [7, 3]))
            self.assertIsNone(load_touch_config(directory, "raspberry_pi_5"))

    def test_detect_flag_is_parsed(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "ttp223.json").write_text(json.dumps({"boards": {
                "orangepi_sun60": {"chip": 0, "lines": [37, 96, 97, 98], "detect": True},
            }}))
            self.assertEqual(load_touch_config(directory, "orangepi_sun60"),
                             TouchConfig(0, [37, 96, 97, 98], detect=True))

    def test_malformed_configuration_is_not_silently_ignored(self):
        entries = [
            {"chip": 0, "lines": []}, {"chip": 0, "lines": [1, 1]},
            {"chip": 0, "lines": [True]}, {"chip": -1, "lines": [1]},
            {"chip": False, "lines": [1]}, {"chip": 0, "lines": [1], "axis": [2]},
            {"chip": 0, "lines": [1], "axis": [1, 1]},
            {"enabled": "false"}, {"chip": 0, "line": 1},
            {"chip": 0, "lines": [1], "detect": "yes"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ttp223.json"
            values = ['{', 'null', '{"boards": []}'] + [
                json.dumps({"boards": {"orangepi_sun60": entry}}) for entry in entries
            ]
            for value in values:
                with self.subTest(value=value):
                    path.write_text(value)
                    with self.assertRaisesRegex(ValueError, "ttp223.json"):
                        load_touch_config(directory, "orangepi_sun60")

    def test_driver_claims_injected_lines_and_copies_axis(self):
        from hal.drivers.ttp223 import TTP223Handler

        config = TouchConfig(2, [3, 7], [7, 3])
        gpio = mock.Mock()
        with mock.patch.dict("sys.modules", {"lgpio": gpio}):
            handler = TTP223Handler(config)
            handler.start()
        gpio.gpiochip_open.assert_called_once_with(2)
        self.assertEqual(gpio.gpio_claim_alert.call_args_list, [
            mock.call(gpio.gpiochip_open.return_value, line, gpio.BOTH_EDGES, gpio.SET_PULL_UP)
            for line in (3, 7)
        ])
        self.assertEqual(handler._axis, [7, 3])
        self.assertIsNot(handler._axis, config.axis)

    def test_detect_probes_with_pull_down_then_claims_every_line(self):
        from hal.drivers.ttp223 import TTP223Handler

        gpio = mock.Mock()
        # Lines 97 and 98 have a TTP223 holding them HIGH; 37 and 96 are bare pins.
        gpio.gpio_read.side_effect = lambda h, line: 1 if line in (97, 98) else 0
        with mock.patch.dict("sys.modules", {"lgpio": gpio}), \
                mock.patch("hal.drivers.ttp223.time.sleep"):
            handler = TTP223Handler(TouchConfig(0, [37, 96, 97, 98], detect=True))
            handler.start()
        handle = gpio.gpiochip_open.return_value
        self.assertEqual(gpio.gpio_claim_input.call_args_list, [
            mock.call(handle, line, gpio.SET_PULL_DOWN) for line in (37, 96, 97, 98)
        ])
        self.assertEqual(gpio.gpio_free.call_count, 4)
        self.assertEqual(handler._active, {97, 98})
        self.assertEqual(handler._wired(), {97, 98})
        self.assertEqual(gpio.gpio_claim_alert.call_count, 4)

    def test_without_detect_every_line_counts_and_nothing_is_probed(self):
        from hal.drivers.ttp223 import TTP223Handler

        gpio = mock.Mock()
        with mock.patch.dict("sys.modules", {"lgpio": gpio}):
            handler = TTP223Handler(TouchConfig(0, [96, 100]))
            handler.start()
        gpio.gpio_claim_input.assert_not_called()
        self.assertIsNone(handler._active)
        self.assertEqual(handler._wired(), {96, 100})

    def test_disabled_driver_never_imports_or_claims_gpio(self):
        from hal.drivers.ttp223 import TTP223Handler

        with mock.patch.dict("sys.modules", {"lgpio": None}):
            TTP223Handler(None).start()

    def test_probe_uses_device_override(self):
        from hal.test_ttp223_probe_orangepi import _wiring

        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory) / "test-body"
            device.mkdir()
            (device / "ttp223.json").write_text(json.dumps({"boards": {
                "orangepi_sun60": {"chip": 2, "lines": [3, 7]},
            }}))
            with mock.patch("hal.board.board.board_profile") as profile:
                profile.return_value.id = "orangepi_sun60"
                self.assertEqual(_wiring("test-body", directory), (2, [3, 7]))
