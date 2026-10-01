#!/usr/bin/python3
"""Early Orange Pi boot indicator, stopped synchronously before HAL starts."""

import math
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def supported():
    try:
        return Path('/proc/device-tree/model').read_text().strip('\x00\n') == 'sun60iw2'
    except OSError:
        return False


def frame(level):
    # Match HAL's SPI timing: 6.4 MHz, GRB, 10 LOW primer + 250 reset bytes.
    byte = bytes(0xFC if level & (1 << bit) else 0xC0 for bit in range(7, -1, -1))
    return bytes(10) + byte * (3 * 32) + bytes(250)


def breathe(spi, stop):
    opened = False
    try:
        spi.open(3, 0)
        opened = True
        spi.max_speed_hz = 6_400_000
        spi.mode = 0
        spi.bits_per_word = 8
        started = time.monotonic()
        while not stop.is_set():
            phase = (time.monotonic() - started) / 3.0
            level = round(1.5 * (1 - math.cos(2 * math.pi * phase)))
            spi.writebytes2(frame(level))
            stop.wait(0.05)
    finally:
        try:
            # No animation worker survives this clear. HAL starts only after exit.
            if opened:
                spi.writebytes2(frame(0))
                time.sleep(0.01)
                spi.writebytes2(frame(0))
                time.sleep(0.01)
                spi.xfer2([0] * 100)
        finally:
            spi.close()


def main():
    if sys.argv[1:] == ['--supported']:
        return 0 if supported() else 1
    if not supported():
        return 0
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    # Guard manual starts after boot; normal ownership is enforced by systemd.
    state = subprocess.check_output(
        ['systemctl', 'show', 'hal.service', '-p', 'ActiveState', '--value'],
        text=True, timeout=2,
    ).strip()
    if state not in ('inactive', 'failed'):
        raise RuntimeError(f'Refusing boot LED while HAL is {state}')
    # udev may still be creating SPI nodes. Do not hold up Linux or HAL for this.
    deadline = time.monotonic() + 10
    while not Path('/dev/spidev3.0').exists():
        if stop.wait(0.1) or time.monotonic() >= deadline:
            return 0
    if not stop.is_set():
        import spidev
        breathe(spidev.SpiDev(), stop)
    return 0


if __name__ == '__main__':
    sys.exit(main())
