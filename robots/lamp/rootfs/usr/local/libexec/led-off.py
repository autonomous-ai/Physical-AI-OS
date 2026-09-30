#!/usr/bin/python3
"""Post-HAL WS2812 blackout for the Orange Pi sun60iw2 lamp.

Keep the off frame identical to the hardware team's ws2812.py: eight LOW
primer bytes, 32 black GRB pixels, and 64 LOW reset bytes at 6.4 MHz.
This fallback does not change the GPIO mux or start an animation.
"""

import subprocess
import sys
from pathlib import Path


def supported():
    try:
        model = Path('/proc/device-tree/model').read_text().strip('\x00\n')
    except OSError:
        return False
    return model == 'sun60iw2' and Path('/dev/spidev3.0').exists()


def clear_strip(spi):
    try:
        spi.open(3, 0)
        spi.max_speed_hz = 6_400_000
        spi.mode = 0
        spi.bits_per_word = 8
        spi.writebytes2(bytearray(8) + bytearray([0xC0] * (32 * 24)) + bytearray(64))
    finally:
        spi.close()


def main():
    if sys.argv[1:] == ['--supported']:
        return 0 if supported() else 1
    if not supported():
        raise RuntimeError('Fallback only supports the sun60iw2 lamp on SPI3.0')
    state = subprocess.check_output(
        ['systemctl', 'show', 'hal.service', '-p', 'ActiveState', '--value'],
        text=True, timeout=2,
    ).strip()
    if state not in ('inactive', 'failed'):
        raise RuntimeError(f'Refusing concurrent LED access while HAL is {state}')
    import spidev

    clear_strip(spidev.SpiDev())
    print('Post-HAL blackout frame sent; physical LED state is not readable', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
