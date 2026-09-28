"""LED effect loops — each function runs in a background thread until stop_event is set or
deadline is reached.
"""

import math
import random
import time
import threading
from typing import Optional

from hal.presets import (
    FX_BLINK, FX_BREATHING, FX_BREATHING_FINE, FX_CANDLE, FX_NOTIFICATION_FLASH,
    FX_PULSE, FX_RAINBOW, FX_SPEAKING_WAVE, FX_SPEAKING_WAVE_RAINBOW,
    RGB_CMD_PAINT, RGB_CMD_SOLID,
)
from hal.board.presets_overlay import DEFAULT_LED_COUNT

CANDLE_FLICKER_MIN = 0.8
CANDLE_REFRESH_HZ = 30


def is_done(deadline: Optional[float], stop_event: threading.Event) -> bool:
    """Return True if the effect should stop."""
    if stop_event.is_set():
        return True
    if deadline is not None and time.monotonic() >= deadline:
        return True
    return False


def hsv_to_rgb(h: float, s: float, v: float) -> tuple:
    """Convert HSV (0-1 range) to RGB (0-255 ints)."""
    if s == 0.0:
        val = int(v * 255)
        return (val, val, val)
    i = int(h * 6.0)
    f = (h * 6.0) - i
    p = int(255 * v * (1.0 - s))
    q = int(255 * v * (1.0 - s * f))
    t = int(255 * v * (1.0 - s * (1.0 - f)))
    v_int = int(255 * v)
    i %= 6
    if i == 0:
        return (v_int, t, p)
    if i == 1:
        return (q, v_int, p)
    if i == 2:
        return (p, v_int, t)
    if i == 3:
        return (p, q, v_int)
    if i == 4:
        return (t, p, v_int)
    return (v_int, p, q)


def run_effect(
    effect: str,
    color: tuple,
    speed: float,
    duration_ms: Optional[int],
    stop_event: threading.Event,
    svc,
    base_color: Optional[tuple] = None,
    brightness: float = 1.0,
    start_at_peak: bool = False,
):
    """Dispatch to the appropriate effect loop. Runs in a background thread.

    Effect loops must pace frames with stop_event.wait(delay), never time.sleep(delay).
    """
    deadline = None
    if duration_ms is not None:
        deadline = time.monotonic() + duration_ms / 1000.0

    try:
        if effect == FX_BREATHING:
            breathing(color, speed, deadline, stop_event, svc, start_at_peak)
        elif effect == FX_BREATHING_FINE:
            breathing_fine(color, speed, deadline, stop_event, svc, start_at_peak)
        elif effect == FX_CANDLE:
            candle(color, speed, deadline, stop_event, svc)
        elif effect == FX_RAINBOW:
            rainbow(speed, deadline, stop_event, svc, brightness)
        elif effect == FX_NOTIFICATION_FLASH:
            notification_flash(color, speed, stop_event, svc)
        elif effect == FX_PULSE:
            pulse(color, speed, deadline, stop_event, svc, base_color or (0, 0, 0))
        elif effect == FX_BLINK:
            blink(color, speed, deadline, stop_event, svc)
        elif effect == FX_SPEAKING_WAVE:
            speaking_wave(color, speed, deadline, stop_event, svc)
        elif effect == FX_SPEAKING_WAVE_RAINBOW:
            speaking_wave_rainbow(speed, deadline, stop_event, svc, brightness)
    except Exception as e:
        import logging
        logging.getLogger("hal.led.effects").warning("LED effect '%s' error: %s", effect, e)


def breathing(
    color: tuple,
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
    start_at_peak: bool = False,
):
    """Fade in/out with the given color."""
    step_delay = 0.03 / speed
    start = 50 if start_at_peak else 0
    while not is_done(deadline, stop_event):
        for i in range(start, 100):
            if is_done(deadline, stop_event):
                return
            brightness = math.sin(math.pi * i / 100.0)
            scaled = tuple(int(c * brightness) for c in color)
            svc.dispatch(RGB_CMD_SOLID, scaled)
            stop_event.wait(step_delay)
        start = 0


_DITHER_STRIDE = 13


def _dither_ring(low: tuple, high: tuple, k: int, n: int) -> list:
    """n pixels, k of them at `high` and the rest at `low`, evenly scattered."""
    if k <= 0:
        return [low] * n
    if k >= n:
        return [high] * n
    return [high if (i * _DITHER_STRIDE) % n < k else low for i in range(n)]


def breathing_fine(
    color: tuple,
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
    start_at_peak: bool = False,
):
    """Breathing whose resolution comes from the ring, not from 8-bit colour."""
    step_delay = 0.03 / speed
    n = getattr(svc, "led_count", DEFAULT_LED_COUNT) or DEFAULT_LED_COUNT
    # One unit below the peak on every channel that is lit: the floor of the
    # breath. Channels that are already 0 stay 0, so the hue never shifts.
    low = tuple(max(c - 1, 0) for c in color)
    start = 50 if start_at_peak else 0
    while not is_done(deadline, stop_event):
        for i in range(start, 100):
            if is_done(deadline, stop_event):
                return
            brightness = math.sin(math.pi * i / 100.0)
            svc.dispatch(RGB_CMD_PAINT, _dither_ring(low, color, round(brightness * n), n))
            stop_event.wait(step_delay)
        start = 0


def candle(
    color: tuple,
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
):
    """Flicker effect: per-pixel brightness varies, hue does NOT.

    Hue swung from the declared 44 deg (yellow) down to 5-20 deg, so the lamp showed a
    scatter of orange pixels and never the color the preset asked for (observed on a
    lamp, 19/08/2026).
    """
    led_count = getattr(svc, "led_count", DEFAULT_LED_COUNT)
    step_delay = 1.0 / CANDLE_REFRESH_HZ
    approach = min(1.0, max(0.02, speed * 0.6))
    levels = [random.uniform(CANDLE_FLICKER_MIN, 1.0) for _ in range(led_count)]
    targets = [random.uniform(CANDLE_FLICKER_MIN, 1.0) for _ in range(led_count)]
    while not is_done(deadline, stop_event):
        pixels = []
        for i in range(led_count):
            level = levels[i] + (targets[i] - levels[i]) * approach
            levels[i] = level
            if abs(targets[i] - level) < 0.01:
                targets[i] = random.uniform(CANDLE_FLICKER_MIN, 1.0)
            pixels.append(tuple(min(255, int(c * level)) for c in color))
        svc.dispatch(RGB_CMD_PAINT, pixels)
        stop_event.wait(step_delay)


def rainbow(
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
    brightness: float = 1.0,
):
    """Cycle through hue spectrum across all pixels."""
    step_delay = 0.03 / speed
    led_count = getattr(svc, "led_count", DEFAULT_LED_COUNT)
    value = max(0.0, min(1.0, brightness))
    offset = 0.0
    while not is_done(deadline, stop_event):
        pixels = []
        for i in range(led_count):
            hue = (offset + i / led_count) % 1.0
            r, g, b = hsv_to_rgb(hue, 1.0, value)
            pixels.append((r, g, b))
        svc.dispatch(RGB_CMD_PAINT, pixels)
        offset += 0.01
        stop_event.wait(step_delay)


def notification_flash(
    color: tuple,
    speed: float,
    stop_event: threading.Event,
    svc,
):
    """3 quick flashes then stop."""
    flash_on = 0.15 / speed
    flash_off = 0.1 / speed
    for _ in range(3):
        if stop_event.is_set():
            return
        svc.dispatch(RGB_CMD_SOLID, color)
        stop_event.wait(flash_on)
        if stop_event.is_set():
            return
        svc.dispatch(RGB_CMD_SOLID, (0, 0, 0))
        stop_event.wait(flash_off)


def blink(
    color: tuple,
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
):
    """Rapid on/off blink. speed=1 → ~3 Hz, speed=2 → ~6 Hz, speed=0.5 → ~1.5 Hz."""
    half_period = 1.0 / (speed * 6.0)
    while not is_done(deadline, stop_event):
        svc.dispatch(RGB_CMD_SOLID, color)
        stop_event.wait(half_period)
        if is_done(deadline, stop_event):
            return
        svc.dispatch(RGB_CMD_SOLID, (0, 0, 0))
        stop_event.wait(half_period)


def pulse(
    color: tuple,
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
    base_color: tuple = (0, 0, 0),
):
    """A pulse of light travelling around the ring, overlaid on a base color.

    The wavefront travels: it used to sit at a fixed origin and expand outward, which on
    a closed ring is two arcs opening in opposite directions — never the one moving
    light the effect is named for.
    """
    step_delay = 0.04 / speed
    led_count = getattr(svc, "led_count", DEFAULT_LED_COUNT)
    width = max(2.0, led_count / 6.0)
    while not is_done(deadline, stop_event):
        for head in range(led_count):
            if is_done(deadline, stop_event):
                return
            pixels = [base_color] * led_count
            for i in range(led_count):
                delta = abs(i - head)
                dist = min(delta, led_count - delta)
                falloff = max(0.0, 1.0 - dist / width)
                if falloff > 0:
                    pixels[i] = tuple(
                        int(base_color[c] + (color[c] - base_color[c]) * falloff)
                        for c in range(3)
                    )
            svc.dispatch(RGB_CMD_PAINT, pixels)
            stop_event.wait(step_delay)


def speaking_wave(
    color: tuple,
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
):
    """Audio-reactive speaking effect — simulated VU meter / equalizer."""
    step_delay = 0.04 / speed
    led_count = getattr(svc, "led_count", DEFAULT_LED_COUNT)
    num_segments = 8
    seg_size = led_count // num_segments

    # Each segment has a current brightness and a target brightness
    current = [0.5] * num_segments
    target = [random.uniform(0.2, 1.0) for _ in range(num_segments)]
    frames_until_new_target = 0

    while not is_done(deadline, stop_event):
        if frames_until_new_target <= 0:
            for s in range(num_segments):
                target[s] = random.uniform(0.0, 1.0)
            frames_until_new_target = random.randint(4, 8)
        frames_until_new_target -= 1

        for s in range(num_segments):
            current[s] += (target[s] - current[s]) * 0.3

        pixels = [(0, 0, 0)] * led_count
        for s in range(num_segments):
            brightness = current[s]
            seg_color = tuple(int(c * brightness) for c in color)
            for p in range(seg_size):
                idx = s * seg_size + p
                if idx < led_count:
                    pixels[idx] = seg_color

        svc.dispatch(RGB_CMD_PAINT, pixels)
        stop_event.wait(step_delay)


def speaking_wave_rainbow(
    speed: float,
    deadline: Optional[float],
    stop_event: threading.Event,
    svc,
    brightness: float = 1.0,
):
    """Same VU-meter motion as speaking_wave, but each segment paints a different hue
    (rainbow palette) that slowly drifts over time.
    """
    step_delay = 0.04 / speed
    led_count = getattr(svc, "led_count", DEFAULT_LED_COUNT)
    num_segments = 8
    seg_size = led_count // num_segments

    current = [0.5] * num_segments
    target = [random.uniform(0.2, 1.0) for _ in range(num_segments)]
    frames_until_new_target = 0
    hue_offset = 0.0
    level = max(0.0, min(1.0, brightness))

    while not is_done(deadline, stop_event):
        if frames_until_new_target <= 0:
            for s in range(num_segments):
                target[s] = random.uniform(0.0, 1.0)
            frames_until_new_target = random.randint(4, 8)
        frames_until_new_target -= 1

        for s in range(num_segments):
            current[s] += (target[s] - current[s]) * 0.3

        pixels = [(0, 0, 0)] * led_count
        for s in range(num_segments):
            brightness = current[s]
            hue = (hue_offset + s / num_segments) % 1.0
            r, g, b = hsv_to_rgb(hue, 1.0, brightness * level)
            seg_color = (r, g, b)
            for p in range(seg_size):
                idx = s * seg_size + p
                if idx < led_count:
                    pixels[idx] = seg_color

        svc.dispatch(RGB_CMD_PAINT, pixels)
        hue_offset = (hue_offset + 0.005) % 1.0
        stop_event.wait(step_delay)
