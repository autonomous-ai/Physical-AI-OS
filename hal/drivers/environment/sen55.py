"""SEN55 I2C protocol (Sensirion SEN5x datasheet, section 6)."""

import os
import time

from hal.drivers.environment.i2c import crc8, decode_words


FIELDS = (
    "pm1_0_ug_m3", "pm2_5_ug_m3", "pm4_0_ug_m3", "pm10_ug_m3",
    "humidity_pct", "temperature_c", "voc_index", "nox_index",
)


class SEN55:
    """Single-worker driver; opens only the configured Linux I2C bus."""

    def __init__(self, bus: int):
        import fcntl

        self._fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        try:
            fcntl.ioctl(self._fd, 0x0703, 0x69)
        except BaseException:
            os.close(self._fd)
            raise

    def _command(self, command: int, count: int = 0, delay: float = 0.02):
        if os.write(self._fd, command.to_bytes(2, "big")) != 2:
            raise OSError("SEN55: incomplete I2C command")
        time.sleep(delay)
        if count:
            return decode_words(os.read(self._fd, count * 3), count)
        return []

    def start(self):
        self._command(0x0104, delay=0.2)
        name = self._command(0xD014, 16)
        product = b"".join(w.to_bytes(2, "big") for w in name).split(b"\0", 1)[0]
        if product != b"SEN55":
            raise OSError(f"Expected SEN55, found {product!r}")
        self._command(0x0021, delay=0.05)

    def read(self):
        if not self._command(0x0202, 1)[0] & 1:
            return None
        words = self._command(0x03C4, 8)
        values = {}
        for index, (field, word, scale) in enumerate(zip(
            FIELDS, words, (10, 10, 10, 10, 100, 200, 10, 10),
        )):
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
            self._command(0x0104, delay=0.2)
        finally:
            os.close(self._fd)
