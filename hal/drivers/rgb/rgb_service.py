import logging
import os
import threading
import time
from typing import Any, List, Union
from ..base import ServiceBase
from hal import config
from hal.presets import RGB_CMD_PAINT, RGB_CMD_SOLID
from hal.safety.policy import clamp_color
from hal.board.board import board_profile

logger = logging.getLogger("hal.rgb")


def _color_tuple(color_code):
    """Convert color (int or RGB tuple) to (r, g, b) tuple."""
    if isinstance(color_code, tuple) and len(color_code) == 3:
        return color_code
    elif isinstance(color_code, int):
        r = (color_code >> 16) & 0xFF
        g = (color_code >> 8) & 0xFF
        b = color_code & 0xFF
        return (r, g, b)
    return None


class _StripPWM:
    """Pi 4 — rpi_ws281x PWM driver on GPIO 12."""

    def __init__(self, led_count, led_pin, led_freq_hz, led_dma,
                 led_invert, led_brightness, led_channel):
        from rpi_ws281x import PixelStrip
        self._strip = PixelStrip(
            led_count, led_pin, led_freq_hz, led_dma,
            led_invert, led_brightness, led_channel,
        )
        self._strip.begin()

    def setPixelColor(self, i, color_tuple):
        from rpi_ws281x import Color
        self._strip.setPixelColor(i, Color(*color_tuple))

    def fill(self, color_tuple, count):
        from rpi_ws281x import Color
        c = Color(*color_tuple)
        for i in range(count):
            self._strip.setPixelColor(i, c)

    def show(self):
        self._strip.show()

    def getPixelColor(self, index):
        raw = self._strip.getPixelColor(index)
        return (raw >> 16) & 0xFF, (raw >> 8) & 0xFF, raw & 0xFF

    def deinit(self):
        pass


class _MemoryStrip:
    """WS2812-compatible strip retained in memory for HAL_SIMULATE."""

    def __init__(self, led_count: int):
        self._pixels = [(0, 0, 0)] * led_count

    def setPixelColor(self, index: int, color_tuple):
        self._pixels[index] = tuple(color_tuple)

    def fill(self, color_tuple, count: int):
        self._pixels[:count] = [tuple(color_tuple)] * count

    def show(self):
        pass

    def getPixelColor(self, index: int):
        return self._pixels[index]

    def deinit(self):
        pass


class _StripSPI:
    """Pi 5 — raw spidev WS2812 driver over SPI0.0 MOSI (GPIO 10)."""

    _BIT0 = 0xC0
    _BIT1 = 0xFC
    _PRIMER_BYTES = 10
    _RESET_BYTES = 250

    def __init__(self, led_count, led_brightness, spi_bus=0, spi_device=0):
        import spidev
        self._led_count = led_count
        self._brightness = led_brightness
        self._pixels = [(0, 0, 0)] * led_count
        self._spi = spidev.SpiDev()
        self._spi.open(spi_bus, spi_device)
        self._spi.max_speed_hz = 6_400_000
        self._spi.mode = 0b00

    def _encode_byte(self, b):
        return [self._BIT1 if b & (1 << (7 - i)) else self._BIT0 for i in range(8)]

    def _encode_pixel(self, r, g, b):
        # WS2812 expects GRB order; apply brightness scaling
        br = self._brightness
        return (self._encode_byte(int(g * br))
                + self._encode_byte(int(r * br))
                + self._encode_byte(int(b * br)))

    def setPixelColor(self, i, color_tuple):
        self._pixels[i] = color_tuple

    def fill(self, color_tuple, count):
        for i in range(count):
            self._pixels[i] = color_tuple

    def show(self):
        buf = [0x00] * self._PRIMER_BYTES
        for r, g, b in self._pixels:
            buf.extend(self._encode_pixel(r, g, b))
        buf.extend([0x00] * self._RESET_BYTES)
        try:
            self._spi.writebytes2(buf)
            logger.debug("SPI write OK — pixel[0]=%s", self._pixels[0] if self._pixels else None)
        except Exception as e:
            logger.error("SPI write failed — LED signal did not reach strip: %s", e)

    def getPixelColor(self, index):
        return self._pixels[index]

    def deinit(self):
        self._spi.close()


class RGBService(ServiceBase):
    """Unified RGB LED service — auto-detects Pi 4 (PWM), Pi 5 (SPI), OrangePi sun60iw2 (SPI3)."""

    def __init__(self,
                 led_count: int = 64,
                 led_pin: int = 12,
                 led_freq_hz: int = 800000,
                 led_dma: int = 10,
                 led_brightness: int = 255,
                 led_invert: bool = False,
                 led_channel: int = 0,
                 safety_policy=None):
        super().__init__("rgb")
        self.led_count = led_count
        self._driver = None
        self._driver_lock = threading.RLock()
        # SAFETY.md brightness ceiling (None = no bound → pass-through). Every
        # pixel write — routes AND effects — funnels through _handle_solid /
        # _handle_paint, so clamping here is the single, bypass-proof gate.
        self._safety = safety_policy

        led = board_profile().led
        try:
            if config.SIMULATE:
                self._driver = _MemoryStrip(led_count)
                self.logger.info("RGB using virtual memory strip")
            elif led.transport == "spi":
                self._driver = _StripSPI(
                    led_count, led_brightness / 255.0,
                    spi_bus=led.spi_bus, spi_device=led.spi_device,
                )
                self.logger.info(
                    "RGB using SPI driver (%s, SPI%d.%d)",
                    board_profile().id, led.spi_bus, led.spi_device,
                )
            else:
                self._driver = _StripPWM(
                    led_count, led_pin, led_freq_hz, led_dma,
                    led_invert, led_brightness, led_channel,
                )
                self.logger.info("RGB using PWM driver (%s)", board_profile().id)
        except Exception as e:
            self.logger.error(f"RGB driver init failed: {e}")

        self.strip = self

        # Blank the strip before anyone can paint it. Device-verified 12/08/2026 on
        # lamp-ac82: strip showed a green arc while GET /led/color reported [0,0,0], and
        # a single clear() wiped it.
        if self._driver:
            try:
                self.clear()
            except Exception as e:
                self.logger.warning("Initial LED clear failed: %s", e)

    def getPixelColor(self, index: int) -> int:
        """Return packed 0xRRGGBB int for server.py compatibility."""
        if not self._driver:
            return 0
        r, g, b = self._driver.getPixelColor(index)
        return (r << 16) | (g << 8) | b

    def handle_event(self, event_type: str, payload: Any):
        if event_type == RGB_CMD_SOLID:
            self._handle_solid(payload)
        elif event_type == RGB_CMD_PAINT:
            self._handle_paint(payload)
        else:
            self.logger.warning(f"Unknown event type: {event_type}")

    def _handle_solid(self, color_code: Union[int, tuple]):
        """Fill entire strip with single color"""
        if not self._driver:
            return
        color = _color_tuple(color_code)
        if color is None:
            self.logger.error(f"Invalid color format: {color_code}")
            return
        color = clamp_color(self._safety, color)  # safety gate (brightness ceiling)
        with self._driver_lock:
            self._driver.fill(color, self.led_count)
            self._driver.show()

        self.logger.debug(f"Applied solid color: {color_code}")

    def _handle_paint(self, colors: List[Union[int, tuple]]):
        """Set individual pixel colors from array"""
        if not self._driver:
            return
        if not isinstance(colors, list):
            self.logger.error(f"Paint payload must be a list, got: {type(colors)}")
            return
        with self._driver_lock:
            max_pixels = min(len(colors), self.led_count)
            for i in range(max_pixels):
                color = _color_tuple(colors[i])
                if color is None:
                    self.logger.warning(f"Invalid color at index {i}: {colors[i]}")
                    continue
                color = clamp_color(self._safety, color)  # safety gate (brightness ceiling)
                self._driver.setPixelColor(i, color)
            self._driver.show()

        self.logger.debug(f"Applied paint pattern with {max_pixels} colors")

    def clear(self):
        """Turn off all LEDs (double show + extra MOSI idle low for reliability)."""
        if not self._driver:
            return
        # Logged before AND after, because a clear that does not take is the one LED
        # fault nobody can see from the outside. Hold the driver lock through both shows
        # and read-back so another frame cannot repaint it mid-check.
        with self._driver_lock:
            before = self._read_brightest_pixel()
            self._driver.fill((0, 0, 0), self.led_count)
            self._driver.show()
            time.sleep(0.01)
            self._driver.show()
            time.sleep(0.01)
            spi = getattr(self._driver, "_spi", None)
            if spi is not None:
                try:
                    spi.xfer2([0x00] * 100)
                except Exception:
                    pass
            after = self._read_brightest_pixel()
            if after is None:
                return
            if after != (0, 0, 0):
                self.logger.error(
                    "LED clear did NOT take: brightest pixel %s -> %s (driver buffer still lit)",
                    before, after,
                )
            elif before != (0, 0, 0):
                self.logger.info("LED cleared: brightest pixel %s -> (0, 0, 0)", before)

    def _read_brightest_pixel(self):
        """The lit-most pixel on the strip, or None when it cannot be read.

        Diagnostics must never be the reason a clear raises — a driver without read-back
        simply reports nothing.
        """
        try:
            pixels = [self._driver.getPixelColor(i) for i in range(self.led_count)]
        except Exception:
            return None
        return max(pixels, key=max) if pixels else None

    def stop(self, timeout: float = 5.0):
        """Override stop to clear LEDs before stopping"""
        self.clear()
        if self._driver:
            self._driver.deinit()
        super().stop(timeout)
