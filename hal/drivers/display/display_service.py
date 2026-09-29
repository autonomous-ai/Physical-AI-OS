"""Display Service — manages the GC9A01 round LCD display."""

import io
import logging
import threading
import time
from typing import Optional

from PIL import Image

from hal.drivers.display.eyes import (
    EyeState, EXPRESSIONS, render_eye, render_info, WIDTH, HEIGHT,
)

logger = logging.getLogger("hal.display")

IDLE_FPS = 2
ACTIVE_FPS = 15
BLINK_INTERVAL_S = 4.0
BLINK_DURATION_S = 0.15


class DisplayMode:
    EYES = "eyes"
    INFO = "info"


class DisplayService:
    """Manages the GC9A01 display with eyes and info modes."""

    def __init__(self):
        self._driver = None  # GC9A01 hardware driver (None on dev machines)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        self._mode = DisplayMode.EYES
        self._eye_state = EyeState()
        self._info_text = ""
        self._info_subtitle = ""
        self._dirty = True
        self._last_blink = time.time()
        self._blink_until = 0.0

        self._last_frame: Optional[Image.Image] = None

        try:
            import gc9a01
            import spidev
            self._driver = gc9a01.GC9A01(
                width=WIDTH,
                height=HEIGHT,
                rotation=0,
            )
            logger.info("GC9A01 display driver initialized")
        except ImportError:
            logger.warning("GC9A01 driver not available — display runs in framebuffer-only mode")
        except Exception as e:
            logger.warning("GC9A01 init failed: %s", e)

    @property
    def available(self) -> bool:
        return True  # always available (renders to framebuffer even without hardware)

    @property
    def mode(self) -> str:
        return self._mode

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="display")
        self._thread.start()
        logger.info("DisplayService started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("DisplayService stopped")

    def set_expression(self, expression: str, pupil_x: float = 0.0, pupil_y: float = 0.0):
        """Set eye expression. Thread-safe."""
        if expression not in EXPRESSIONS:
            logger.warning("Unknown expression: %s", expression)
            return
        with self._lock:
            self._mode = DisplayMode.EYES
            self._eye_state.expression = expression
            self._eye_state.pupil_x = max(-1.0, min(1.0, pupil_x))
            self._eye_state.pupil_y = max(-1.0, min(1.0, pupil_y))
            self._eye_state.openness = 1.0
            self._dirty = True

    def set_info(self, text: str, subtitle: str = ""):
        """Switch to info mode with text content."""
        with self._lock:
            self._mode = DisplayMode.INFO
            self._info_text = text
            self._info_subtitle = subtitle
            self._dirty = True

    def set_eyes_mode(self):
        """Switch back to eyes mode."""
        with self._lock:
            self._mode = DisplayMode.EYES
            self._dirty = True

    def get_snapshot_bytes(self) -> Optional[bytes]:
        """Get last rendered frame as JPEG bytes."""
        with self._lock:
            if self._last_frame is None:
                return None
            buf = io.BytesIO()
            self._last_frame.save(buf, format="JPEG", quality=85)
            return buf.getvalue()

    def get_state(self) -> dict:
        with self._lock:
            return {
                "mode": self._mode,
                "expression": self._eye_state.expression,
                "pupil_x": self._eye_state.pupil_x,
                "pupil_y": self._eye_state.pupil_y,
                "openness": self._eye_state.openness,
                "available_expressions": list(EXPRESSIONS.keys()),
                "hardware": self._driver is not None,
            }

    def _loop(self):
        """Render loop — pushes frames to display."""
        time.sleep(1)

        while self._running:
            try:
                fps = ACTIVE_FPS if self._dirty else IDLE_FPS
                self._handle_blink()
                self._render()
                time.sleep(1.0 / fps)
            except Exception as e:
                logger.error("Display render error: %s", e)
                time.sleep(1)

    def _handle_blink(self):
        """Auto-blink in eyes mode."""
        now = time.time()
        with self._lock:
            if self._mode != DisplayMode.EYES:
                return

            if now < self._blink_until:
                self._eye_state.openness = 0.05
                self._dirty = True
                return

            if self._eye_state.openness < 1.0:
                self._eye_state.openness = 1.0
                self._dirty = True

            if now - self._last_blink >= BLINK_INTERVAL_S:
                self._blink_until = now + BLINK_DURATION_S
                self._last_blink = now

    def _render(self):
        """Render current state to display."""
        with self._lock:
            if not self._dirty:
                return
            self._dirty = False

            if self._mode == DisplayMode.EYES:
                frame = render_eye(self._eye_state)
            else:
                frame = render_info(self._info_text, self._info_subtitle)

            self._last_frame = frame

        if self._driver:
            try:
                self._driver.display(frame)
            except Exception as e:
                logger.error("Display push failed: %s", e)
