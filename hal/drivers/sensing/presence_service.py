"""
Presence Service — state machine for automatic light on/off based on motion detection.

States:
  PRESENT  — someone is here, lights on (last scene or default)
  IDLE     — no motion for config.IDLE_TIMEOUT_S, dim to config.IDLE_BRIGHTNESS
  AWAY     — no motion for config.AWAY_TIMEOUT_S, lights off

Transitions:
  motion detected or user activity (voice turn, button/touch) → PRESENT (turn on / restore)
  neither for config.IDLE_TIMEOUT_S → IDLE (dim)
  neither for config.AWAY_TIMEOUT_S → AWAY (off)

Calls HAL LED endpoints directly (same process, via rgb_service reference).
"""

import logging
import time
from enum import Enum

import requests

import hal.config as config
from hal.presets import RGB_CMD_SOLID

logger = logging.getLogger("hal.presence")


class PresenceState(str, Enum):
    PRESENT = "present"
    IDLE = "idle"
    AWAY = "away"
    DISABLED = "disabled"


class PresenseService:
    """Tracks presence state based on motion events. Controls LED via rgb_service."""

    def __init__(self, rgb_service=None, send_event=None, auto_enabled: bool = True):
        self._rgb_service = rgb_service
        self._send_event = send_event
        # The idle→away→sleep state machine is driven by on_motion(), fed by the
        # people-perception processors (face / motion / emotion) gated on the
        # `presence` capability, and by on_activity() (voice turns, gestures),
        # which only refreshes a machine that is already enabled. A device that doesn't
        # declare `presence` has no motion source, so leaving auto-control on
        # would make it falsely transition to AWAY (lights off + sleep announce)
        # after the timeout despite being unable to sense anyone. Start disabled
        # in that case; manual /presence/enable can still turn it on.
        self._enabled = auto_enabled
        self._state = PresenceState.PRESENT if auto_enabled else PresenceState.DISABLED
        self._last_motion_time: float = time.time()

        # Guard mode cache — checked periodically from the OS server API
        self._guard_mode: bool = False
        self._guard_last_check: float = 0.0

        # Last known scene color (before dimming/off) so we can restore
        self._last_color: tuple = (255, 180, 100)  # default warm white

    @property
    def state(self) -> PresenceState:
        return self._state

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self):
        self._enabled = True
        self._state = PresenceState.PRESENT
        self._last_motion_time = time.time()
        logger.info("Presence auto-control enabled")

    def disable(self):
        self._enabled = False
        self._state = PresenceState.DISABLED
        logger.info("Presence auto-control disabled")

    def set_last_color(self, color: tuple):
        """Called whenever LED color is set (from scene or manual), so we know what to restore."""
        self._last_color = color

    def on_motion(self):
        """Called by SensingService when motion is detected."""
        self._mark_present("motion detected")

    def on_activity(self, source: str):
        """Called on direct user interaction: a voice turn or a physical gesture.

        The camera alone cannot prove absence. A user talking to the device with
        the camera off (privacy / manual) or sitting outside the frame is still
        here, yet the timer only saw faces, so the device dimmed, went AWAY and
        announced sleep mid-conversation. Interaction resets the same clock.
        """
        self._mark_present(f"user activity: {source}")

    def on_wake(self):
        """Sleep ended: start a fresh idle→away countdown from now.

        Only a face used to reset the clock, so a wake that nobody stood in
        front of (web UI, API, an agent reply to a chat) kept the pre-sleep
        timestamp: already past AWAY_TIMEOUT_S it announced sleep again right
        after waking, and a machine left AWAY never timed out again.
        The light is left alone — the wake emotion owns the strip.
        """
        if not self._enabled:
            return
        self._last_motion_time = time.time()
        if self._state != PresenceState.PRESENT:
            logger.info("Presence: %s → PRESENT (woke from sleep)", self._state)
        self._state = PresenceState.PRESENT

    def _mark_present(self, reason: str):
        if not self._enabled:
            return

        self._last_motion_time = time.time()
        prev_state = self._state

        if prev_state in (PresenceState.IDLE, PresenceState.AWAY):
            self._state = PresenceState.PRESENT
            logger.info(
                "Presence: %s → PRESENT (%s, restoring light)", prev_state, reason
            )
            self._restore_light()

    def _is_guard_mode(self) -> bool:
        """Check guard mode from the OS server API, cached for _GUARD_CHECK_INTERVAL_S."""
        now = time.time()
        if now - self._guard_last_check < config.GUARD_CHECK_INTERVAL_S:
            return self._guard_mode
        self._guard_last_check = now
        try:
            resp = requests.get(config.GUARD_STATUS_URL, timeout=2)
            if resp.status_code == 200:
                self._guard_mode = resp.json().get("data", {}).get("guard_mode", False)
        except Exception:
            pass  # keep last known value
        return self._guard_mode

    def tick(self):
        """Called periodically by sensing loop to check timeouts."""
        if not self._enabled or self._state == PresenceState.DISABLED:
            return

        # Asleep: sleep already owns the light and the speaker, and the camera
        # is off so nothing can reset the clock. Counting on would dim a
        # sleeping lamp back to 20% of the user's colour and leave the machine
        # AWAY for the wake to inherit. on_wake() restarts the countdown.
        if self._is_sleeping():
            return

        # Guard mode: never transition to IDLE or AWAY — the device must stay alert.
        if self._is_guard_mode():
            return

        elapsed = time.time() - self._last_motion_time

        if self._state == PresenceState.PRESENT and elapsed >= config.IDLE_TIMEOUT_S:
            self._state = PresenceState.IDLE
            logger.info("Presence: PRESENT → IDLE (no motion for %ds)", int(elapsed))
            self._dim_light()

        elif self._state == PresenceState.IDLE and elapsed >= config.AWAY_TIMEOUT_S:
            self._state = PresenceState.AWAY
            logger.info("Presence: IDLE → AWAY (no motion for %ds)", int(elapsed))
            self._turn_off_light()
            self._notify_away(int(elapsed))

    @staticmethod
    def _is_sleeping() -> bool:
        """Sleep owns the strip; presence must not relight a sleeping device.
        A gesture reaches on_activity() BEFORE its wake runs (single click),
        and the camera can stay on while asleep under a manual override."""
        try:
            from hal import app_state

            return bool(app_state._sleeping)
        except Exception:
            return False

    @staticmethod
    def _light_is_off() -> bool:
        """True when the strip must be left dark — the user turned the light
        off, or nothing has asked for light and the resting look is dark.
        Presence reacts to a body walking past, which is NOT the user asking
        for light: without this, walking past a lamp that was turned off lights
        it back up at full brightness. Fails open (paints) if app_state can't
        be read, matching the pre-existing behaviour."""
        try:
            from hal.app_state import led_should_stay_dark

            return led_should_stay_dark()
        except Exception:
            return False

    def _restore_light(self):
        """Restore last known color at full brightness.

        Light only. This used to re-aim the head to the active scene direction
        on every IDLE/AWAY -> PRESENT, which killed whatever recording was
        playing and parked the arm as `__aim_hold__` for 5s (#314). Scene
        activation still aims; a presence transition is not one.
        """
        if not self._rgb_service:
            logger.warning("Presence: cannot restore light — rgb_service not available")
            return
        if self._is_sleeping():
            logger.info("Presence: light restore skipped — device is sleeping")
        elif self._light_is_off():
            logger.info("Presence: light restore skipped — strip is off/resting dark")
        else:
            try:
                logger.info("Presence: restoring light color=%s", self._last_color)
                self._rgb_service.dispatch(RGB_CMD_SOLID, self._last_color)
            except Exception as e:
                logger.warning("Presence: failed to restore light: %s", e)

    def _dim_light(self):
        """Dim to config.IDLE_BRIGHTNESS of last color."""
        if not self._rgb_service:
            return
        if self._light_is_off():
            return
        try:
            dimmed = tuple(int(c * config.IDLE_BRIGHTNESS) for c in self._last_color)
            self._rgb_service.dispatch(RGB_CMD_SOLID, dimmed)
        except Exception as e:
            logger.warning("Presence: failed to dim light: %s", e)

    def _notify_away(self, elapsed_s: int):
        """Send presence.away event so agent can announce sleep via TTS + Telegram."""
        if not self._send_event:
            return
        minutes = elapsed_s // 60
        self._send_event(
            "presence.away",
            f"No one has been around for {minutes} minute(s). "
            f"You're going to sleep — lights off. "
            f"Announce that you're going to sleep in a cozy, sleepy way.",
            cooldown=config.AWAY_TIMEOUT_S,
        )

    def _turn_off_light(self):
        """Turn off LEDs."""
        if not self._rgb_service:
            return
        try:
            self._rgb_service.clear()
        except Exception as e:
            logger.warning("Presence: failed to turn off light: %s", e)

    def to_dict(self) -> dict:
        return {
            "state": self._state.value,
            "enabled": self._enabled,
            "seconds_since_motion": int(time.time() - self._last_motion_time),
            "idle_timeout": config.IDLE_TIMEOUT_S,
            "away_timeout": config.AWAY_TIMEOUT_S,
        }
