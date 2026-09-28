"""SEN63C protocol; see Sensirion/python-i2c-sen63c commands.py.

This model shares the SEN6x address but has its own seven-word read command.
"""

import os
import time

from hal.drivers.environment.i2c import crc8, decode_words, limit_sunxi_bus_clock


FIELDS = (
    "pm1_0_ug_m3", "pm2_5_ug_m3", "pm4_0_ug_m3", "pm10_ug_m3",
    "humidity_pct", "temperature_c", "co2_ppm",
)


class SEN63C:
    """Single-worker Linux I2C acquisition; preserves calibration by default."""

    def __init__(self, bus: int, *, automatic_self_calibration: bool | None = None):
        import fcntl

        if automatic_self_calibration is not None and type(automatic_self_calibration) is not bool:
            raise ValueError("automatic_self_calibration must be a boolean or null")
        self._automatic_self_calibration = automatic_self_calibration
        # SEN6x supports standard mode only; Sunxi defaults can be 400 kHz.
        limit_sunxi_bus_clock(bus, 100_000)
        self._fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        try:
            fcntl.ioctl(self._fd, 0x0703, 0x6B)  # I2C_SLAVE, not FORCE.
        except BaseException:
            os.close(self._fd)
            raise

    def _command(self, command: int, count: int = 0, delay: float = 0.02, word=None):
        payload = command.to_bytes(2, "big")
        if word is not None:
            pair = word.to_bytes(2, "big")
            payload += pair + bytes([crc8(pair)])
        if os.write(self._fd, payload) != len(payload):
            raise OSError("SEN63C: incomplete I2C command")
        time.sleep(delay)
        if count:
            return decode_words(os.read(self._fd, count * 3), count, "SEN63C")
        return []

    def start(self):
        # Recover a previous measuring process without resetting calibration.
        self._command(0x0104, delay=1.4)
        words = self._command(0xD002, 16)
        product = b"".join(w.to_bytes(2, "big") for w in words).split(b"\0", 1)[0]
        if product != b"00085700":
            raise OSError(f"Expected SEN63C product type 00085700, found {product!r}")
        if self._automatic_self_calibration is not None:
            self._command(0x6711, word=int(self._automatic_self_calibration))
        self._command(0x0021, delay=0.05)

    def read(self):
        if not self._command(0x0202, 1)[0] & 0xFF:
            return None
        words = self._command(0x0471, 7)
        values = {}
        for index, (field, word, scale) in enumerate(zip(
            FIELDS, words, (10, 10, 10, 10, 100, 200, 1),
        )):
            # CO2 remains unknown during its initial 22–24 seconds; other
            # metrics can already be valid and must still reach the group.
            if word == (0xFFFF if index < 4 else 0x7FFF):
                values[field] = None
            else:
                signed = word - 65536 if index >= 4 and word & 0x8000 else word
                values[field] = signed / scale
        status = self._command(0xD206, 2)
        return {"timestamp": time.time(), **values,
                "device_status": (status[0] << 16) | status[1]}

    def close(self):
        try:
            self._command(0x0104, delay=1.4)
        finally:
            os.close(self._fd)
