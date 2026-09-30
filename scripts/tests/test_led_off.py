"""The shutdown fallback must never compete with HAL or hide SPI failures."""

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[2] / 'robots/lamp/rootfs/usr/local/libexec/led-off.py'
spec = importlib.util.spec_from_file_location('led_off', SCRIPT)
off = importlib.util.module_from_spec(spec)
spec.loader.exec_module(off)


class ShutdownOffTests(unittest.TestCase):
    def test_frame_matches_hardware_blackout(self):
        spi = Mock()
        off.clear_strip(spi)
        spi.open.assert_called_once_with(3, 0)
        self.assertEqual((spi.max_speed_hz, spi.mode, spi.bits_per_word), (6_400_000, 0, 8))
        spi.writebytes2.assert_called_once_with(bytearray(8) + bytearray([0xC0] * 768) + bytearray(64))
        self.assertEqual(spi.method_calls[-1][0], 'close')

    def test_write_failure_is_visible_and_handle_is_closed(self):
        spi = Mock()
        spi.writebytes2.side_effect = OSError('SPI unavailable')
        with self.assertRaises(OSError):
            off.clear_strip(spi)
        spi.close.assert_called_once()

    def test_live_or_stopping_hal_cannot_be_overwritten(self):
        for state in ('active', 'activating', 'deactivating', 'unknown'):
            with self.subTest(state=state), patch.object(off, 'supported', return_value=True), \
                    patch.object(sys, 'argv', [str(SCRIPT)]), \
                    patch.object(off.subprocess, 'check_output', return_value=state), \
                    patch.dict(sys.modules, {'spidev': Mock()}) as modules:
                with self.assertRaisesRegex(RuntimeError, 'Refusing concurrent'):
                    off.main()
                modules['spidev'].SpiDev.assert_not_called()

    def test_unsupported_board_skips_service_without_opening_spi(self):
        with patch.object(off, 'supported', return_value=False), \
                patch.object(sys, 'argv', [str(SCRIPT), '--supported']), \
                patch.object(off.subprocess, 'check_output') as command:
            self.assertEqual(off.main(), 1)
            command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
