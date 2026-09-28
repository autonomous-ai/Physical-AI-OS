"""Sensirion word CRC framing shared by environmental I2C drivers."""

import logging
from pathlib import Path
import re


logger = logging.getLogger(__name__)


def limit_sunxi_bus_clock(bus: int, max_hz: int, *, sysfs_root=Path("/sys/class/i2c-adapter")):
    """Limit a Sunxi adapter's shared clock before accessing a slow sensor.

    Other controllers require their platform's clock configuration. The Sunxi
    kernel exposes a write-only freq attribute and reports it in device/info.
    Do not use the device-tree clock-frequency as runtime readback.
    """
    adapter = sysfs_root / f"i2c-{bus}"
    try:
        name = (adapter / "name").read_text().strip()
    except FileNotFoundError:
        return
    if not name.startswith("SUNXI TWI"):
        return

    def read_frequency():
        # "freqency" is the spelling in the Sunxi kernel ABI.
        info = (adapter / "device/info").read_text()
        match = re.search(r"^twi->freqency\s*=\s*(\d+)\s*$", info, re.MULTILINE)
        if not match or int(match[1]) <= 0:
            raise OSError(f"I2C bus {bus}: cannot determine Sunxi clock frequency")
        return int(match[1])

    try:
        previous = read_frequency()
        if previous <= max_hz:
            return
        (adapter / "device/freq").write_text(f"{max_hz}\n")
        actual = read_frequency()
        if actual > max_hz:
            raise OSError(f"clock remains {actual} Hz after requesting {max_hz} Hz")
        logger.info("I2C bus %s: limited shared Sunxi clock from %s to %s Hz", bus, previous, actual)
    except OSError as exc:
        raise OSError(f"I2C bus {bus}: cannot enforce maximum {max_hz} Hz: {exc}") from exc


def crc8(data: bytes) -> int:
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ (0x31 if crc & 0x80 else 0)) & 0xFF
    return crc


def decode_words(data: bytes, count: int, sensor: str = "SEN55") -> list[int]:
    if len(data) != count * 3:
        raise OSError(f"{sensor}: incomplete I2C response")
    words = []
    for offset in range(0, len(data), 3):
        pair = data[offset:offset + 2]
        if crc8(pair) != data[offset + 2]:
            raise OSError(f"{sensor}: CRC mismatch")
        words.append(int.from_bytes(pair, "big"))
    return words
