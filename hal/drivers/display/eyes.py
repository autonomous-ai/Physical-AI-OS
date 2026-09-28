"""Pixel Art Eyes — renders expressive eyes on the GC9A01 240x240 round LCD."""

from PIL import Image, ImageDraw, ImageFont
from typing import Optional, Tuple

from hal.presets import (
    EMO_CURIOUS, EMO_EXCITED, EMO_HAPPY, EMO_SAD, EMO_SHOCK,
    EMO_SHY, EMO_SLEEPY, EMO_THINKING,
)

WIDTH = 240
HEIGHT = 240
CENTER_X = WIDTH // 2
CENTER_Y = HEIGHT // 2

BG_COLOR = (0, 0, 0)
EYE_WHITE = (240, 240, 240)
PUPIL_COLOR = (40, 40, 40)
IRIS_COLOR = (80, 160, 255)
EYELID_COLOR = (0, 0, 0)

EYE_WIDTH = 140
EYE_HEIGHT = 120
PUPIL_RADIUS = 22
IRIS_RADIUS = 38

PUPIL_MAX_OFFSET_X = 30
PUPIL_MAX_OFFSET_Y = 20


class EyeState:
    """Current state of the eye display."""

    def __init__(self):
        self.expression: str = "neutral"
        self.pupil_x: float = 0.0
        self.pupil_y: float = 0.0
        self.openness: float = 1.0
        self.blink: bool = False


EXPRESSIONS = {
    "neutral":     {"eye_h_scale": 1.0, "pupil_y": 0.0, "squint": 0.0},
    EMO_HAPPY:     {"eye_h_scale": 0.6, "pupil_y": 0.1, "squint": 0.3},
    EMO_SAD:       {"eye_h_scale": 0.8, "pupil_y": 0.3, "squint": 0.0, "droop_top": True},
    EMO_CURIOUS:   {"eye_h_scale": 1.2, "pupil_y": -0.1, "squint": 0.0},
    EMO_THINKING:  {"eye_h_scale": 0.9, "pupil_x": 0.5, "pupil_y": -0.3, "squint": 0.1},
    EMO_EXCITED:   {"eye_h_scale": 1.3, "pupil_y": 0.0, "squint": 0.0},
    EMO_SHY:       {"eye_h_scale": 0.5, "pupil_x": -0.4, "pupil_y": 0.2, "squint": 0.2},
    EMO_SHOCK:     {"eye_h_scale": 1.4, "pupil_y": 0.0, "squint": 0.0},
    EMO_SLEEPY:    {"eye_h_scale": 0.3, "pupil_y": 0.2, "squint": 0.5},
    "angry":       {"eye_h_scale": 0.7, "pupil_y": 0.0, "squint": 0.2, "angry_brow": True},
    "love":        {"eye_h_scale": 0.8, "pupil_y": 0.0, "squint": 0.1, "heart": True},
}


def render_eye(state: EyeState) -> Image.Image:
    """Render a single eye frame as a 240x240 PIL Image."""
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    expr = EXPRESSIONS.get(state.expression, EXPRESSIONS["neutral"])

    eye_h_scale = expr.get("eye_h_scale", 1.0)
    squint = expr.get("squint", 0.0)
    ew = EYE_WIDTH
    eh = int(EYE_HEIGHT * eye_h_scale * state.openness)

    if eh < 4:
        draw.line(
            [(CENTER_X - ew // 2, CENTER_Y), (CENTER_X + ew // 2, CENTER_Y)],
            fill=EYE_WHITE, width=3,
        )
        return img

    eye_left = CENTER_X - ew // 2
    eye_top = CENTER_Y - eh // 2
    eye_right = CENTER_X + ew // 2
    eye_bottom = CENTER_Y + eh // 2
    corner_radius = min(ew, eh) // 3

    draw.rounded_rectangle(
        [eye_left, eye_top, eye_right, eye_bottom],
        radius=corner_radius,
        fill=EYE_WHITE,
    )

    if squint > 0:
        squint_px = int(eh * squint * 0.5)
        draw.rectangle(
            [eye_left - 5, eye_top - 5, eye_right + 5, eye_top + squint_px],
            fill=BG_COLOR,
        )
        draw.rectangle(
            [eye_left - 5, eye_bottom - squint_px, eye_right + 5, eye_bottom + 5],
            fill=BG_COLOR,
        )

    if expr.get("droop_top"):
        for i in range(20):
            x = eye_left + i * (ew // 20)
            y = eye_top + int(i * 0.8)
            draw.rectangle([x, eye_top - 5, x + ew // 20 + 1, y], fill=BG_COLOR)

    if expr.get("angry_brow"):
        draw.line(
            [(eye_left + 10, eye_top - 15), (eye_right - 10, eye_top - 5)],
            fill=(200, 50, 50), width=5,
        )

    px_offset = expr.get("pupil_x", 0.0) + state.pupil_x
    py_offset = expr.get("pupil_y", 0.0) + state.pupil_y
    px = CENTER_X + int(px_offset * PUPIL_MAX_OFFSET_X)
    py = CENTER_Y + int(py_offset * PUPIL_MAX_OFFSET_Y)

    px = max(eye_left + IRIS_RADIUS, min(eye_right - IRIS_RADIUS, px))
    py = max(eye_top + IRIS_RADIUS, min(eye_bottom - IRIS_RADIUS, py))

    if expr.get("heart"):
        _draw_heart(draw, px, py, IRIS_RADIUS, (255, 80, 120))
    else:
        draw.ellipse(
            [px - IRIS_RADIUS, py - IRIS_RADIUS, px + IRIS_RADIUS, py + IRIS_RADIUS],
            fill=IRIS_COLOR,
        )
        draw.ellipse(
            [px - PUPIL_RADIUS, py - PUPIL_RADIUS, px + PUPIL_RADIUS, py + PUPIL_RADIUS],
            fill=PUPIL_COLOR,
        )
        hl_x = px - PUPIL_RADIUS // 2
        hl_y = py - PUPIL_RADIUS // 2
        draw.ellipse(
            [hl_x, hl_y, hl_x + 8, hl_y + 8],
            fill=(255, 255, 255),
        )

    mask = Image.new("L", (WIDTH, HEIGHT), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse([0, 0, WIDTH - 1, HEIGHT - 1], fill=255)
    img.putalpha(mask)

    return img.convert("RGB")


def render_info(text: str, subtitle: str = "", bg_color: Tuple[int, int, int] = (0, 0, 0)) -> Image.Image:
    """Render info mode — text centered on display."""
    img = Image.new("RGB", (WIDTH, HEIGHT), bg_color)
    draw = ImageDraw.Draw(img)

    try:
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
    except (IOError, OSError):
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font_large)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(
        ((WIDTH - tw) // 2, (HEIGHT - th) // 2 - 15),
        text, fill=(255, 255, 255), font=font_large,
    )

    if subtitle:
        bbox2 = draw.textbbox((0, 0), subtitle, font=font_small)
        sw = bbox2[2] - bbox2[0]
        draw.text(
            ((WIDTH - sw) // 2, (HEIGHT + th) // 2 + 10),
            subtitle, fill=(180, 180, 180), font=font_small,
        )

    mask = Image.new("L", (WIDTH, HEIGHT), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse([0, 0, WIDTH - 1, HEIGHT - 1], fill=255)
    img.putalpha(mask)

    return img.convert("RGB")


def _draw_heart(draw: ImageDraw.Draw, cx: int, cy: int, size: int, color: Tuple[int, int, int]):
    """Draw a simple heart shape centered at (cx, cy)."""
    s = size
    draw.ellipse([cx - s, cy - s, cx, cy], fill=color)
    draw.ellipse([cx, cy - s, cx + s, cy], fill=color)
    draw.polygon([(cx - s, cy - s // 4), (cx + s, cy - s // 4), (cx, cy + s)], fill=color)
