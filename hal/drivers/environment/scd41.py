"""SCD41 periodic CO2 acquisition (Sensirion SCD4x datasheet section 3)."""

import os
import time

from hal.drivers.environment.i2c import crc8, decode_words


class SCD41:
    """Single-worker Linux I2C driver; calibration is preserved unless configured."""

    def __init__(self, bus: int, *, automatic_self_calibration: bool | None = None):
        import fcntl

        if automatic_self_calibration is not None and type(automatic_self_calibration) is not bool:
            raise ValueError("automatic_self_calibration must be a boolean or null")
        self._automatic_self_calibration = automatic_self_calibration
        self._fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        try:
            fcntl.ioctl(self._fd, 0x0703, 0x62)
        except BaseException:
            os.close(self._fd)
            raise

    def _command(self, command: int, count: int = 0, delay: float = 0.001, word=None):
        payload = command.to_bytes(2, "big")
        if word is not None:
            pair = word.to_bytes(2, "big")
            payload += pair + bytes([crc8(pair)])
        if os.write(self._fd, payload) != len(payload):
            raise OSError("SCD41: incomplete I2C command")
        time.sleep(delay)
        if count:
            return decode_words(os.read(self._fd, count * 3), count, "SCD41")
        return []

    def start(self):
        self._command(0x3F86, delay=0.5)
        if self._automatic_self_calibration is not None:
            self._command(0x2416, word=int(self._automatic_self_calibration))
        self._command(0x21B1)

    def read(self):
        if not self._command(0xE4B8, 1)[0] & 0x07FF:
            return None
        co2, _, _ = self._command(0xEC05, 3)
        if co2 == 0:
            return None
        return {"timestamp": time.time(), "co2_ppm": co2}

    def close(self):
        try:
            self._command(0x3F86, delay=0.5)
        finally:
            os.close(self._fd)
