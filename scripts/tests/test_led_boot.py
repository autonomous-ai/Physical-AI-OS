"""Boot LED handoff must terminate all writes before HAL starts."""

import importlib.util
import configparser
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[2] / 'robots/lamp/rootfs/usr/local/libexec/led-boot.py'
spec = importlib.util.spec_from_file_location('led_boot', SCRIPT)
boot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boot)


class BootLEDTests(unittest.TestCase):
    def test_shutdown_blackout_is_ordered_after_both_spi_writers(self):
        units = SCRIPT.parents[3] / 'etc/systemd/system'
        off_unit = configparser.ConfigParser(interpolation=None, strict=False)
        off_unit.read(units / 'led-shutdown.service')
        boot_unit = configparser.ConfigParser(interpolation=None, strict=False)
        boot_unit.read(units / 'led-boot.service')
        # systemd reverses Before on stop, even when HAL is already inactive.
        before = off_unit['Unit']['Before'].split()
        self.assertIn('led-boot.service', before)
        self.assertIn('hal.service', before)
        self.assertIn('led-shutdown.service', boot_unit['Unit']['Wants'].split())

    def test_white_peak_matches_hal_encoding(self):
        self.assertEqual(boot.frame(3), bytes(10) + bytes([0xC0] * 6 + [0xFC] * 2) * 96 + bytes(250))
        self.assertEqual(boot.frame(0), bytes(10) + bytes([0xC0] * 768) + bytes(250))

    def test_handoff_stops_animation_clears_and_closes(self):
        stop = threading.Event()
        spi = Mock()
        spi.writebytes2.side_effect = lambda _: stop.set()
        with patch.object(boot.time, 'monotonic', side_effect=[0, 1.5]), \
                patch.object(boot.time, 'sleep'):
            boot.breathe(spi, stop)
        self.assertEqual([call.args[0] for call in spi.writebytes2.call_args_list],
                         [boot.frame(3), boot.frame(0), boot.frame(0)])
        spi.open.assert_called_once_with(3, 0)
        spi.xfer2.assert_called_once_with([0] * 100)
        self.assertEqual(spi.method_calls[-1][0], 'close')

    def test_spi_failure_still_closes(self):
        spi = Mock()
        spi.writebytes2.side_effect = OSError('SPI failure')
        with self.assertRaises(OSError):
            boot.breathe(spi, threading.Event())
        spi.close.assert_called_once()

    def test_open_failure_does_not_write(self):
        spi = Mock()
        spi.open.side_effect = OSError('SPI missing')
        with self.assertRaises(OSError):
            boot.breathe(spi, threading.Event())
        spi.writebytes2.assert_not_called()
        spi.close.assert_called_once()

    def test_cannot_start_over_active_or_starting_hal(self):
        for state in ('active', 'activating', 'deactivating', 'unknown'):
            with self.subTest(state=state), patch.object(boot, 'supported', return_value=True), \
                    patch.object(sys, 'argv', [str(SCRIPT)]), \
                    patch.object(boot.signal, 'signal'), \
                    patch.object(boot.subprocess, 'check_output', return_value=state), \
                    patch.object(boot, 'breathe') as breathe:
                with self.assertRaisesRegex(RuntimeError, 'Refusing boot LED'):
                    boot.main()
                breathe.assert_not_called()

    def test_stop_while_waiting_for_spi_never_opens_driver(self):
        stop = Mock()
        stop.wait.return_value = True
        with patch.object(boot, 'supported', return_value=True), \
                patch.object(sys, 'argv', [str(SCRIPT)]), \
                patch.object(boot.signal, 'signal'), \
                patch.object(boot.threading, 'Event', return_value=stop), \
                patch.object(boot.subprocess, 'check_output', return_value='inactive'), \
                patch.object(boot.Path, 'exists', return_value=False), \
                patch.object(boot, 'breathe') as breathe:
            self.assertEqual(boot.main(), 0)
            breathe.assert_not_called()

    def test_other_boards_skip(self):
        with patch.object(boot, 'supported', return_value=False), \
                patch.object(sys, 'argv', [str(SCRIPT), '--supported']), \
                patch.object(boot.subprocess, 'check_output') as command:
            self.assertEqual(boot.main(), 1)
            command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
