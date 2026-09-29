"""Media handover with Pollen Robotics' reachy_mini daemon (Reachy Mini)."""
from __future__ import annotations

import logging
import os
import time

import requests

from hal.board.device import DEFAULT_STARTUP_VOLUME

logger = logging.getLogger(__name__)


class PollenDaemonMediaOwner:
    """MediaOwner backed by the reachy_mini daemon's /api/media endpoints."""

    _TIMEOUT_S = 5.0
    # The daemon is a systemd service starting alongside HAL, so its HTTP port
    # may not be listening yet on a cold boot. Retry rather than lose the camera
    # and mic for the whole session over a two-second race.
    _RELEASE_ATTEMPTS = 5
    _RETRY_DELAY_S = 2.0

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        startup_volume: int | None = None,
    ):
        self._host = host or os.getenv("REACHY_DAEMON_HOST", "localhost")
        self._port = int(port or os.getenv("REACHY_DAEMON_PORT", "8000"))
        self._base = f"http://{self._host}:{self._port}/api/media"
        self._startup_volume = (
            startup_volume if startup_volume is not None else DEFAULT_STARTUP_VOLUME
        )

    def _post(self, action: str) -> bool:
        resp = requests.post(f"{self._base}/{action}", timeout=self._TIMEOUT_S)
        resp.raise_for_status()
        logger.info("[media-owner] pollen %s -> %s", action, resp.text.strip()[:200])
        return True

    def _restore_volume(self) -> None:
        """Put the user's speaker level back after a release.

        With no persisted level yet — a new owner's first boot, or any unit that never
        touched the slider — falls back to the body's declared `startup_volume`.
        """
        pct = None
        try:
            from hal.config import VOLUME_STATE_PATH
            with open(VOLUME_STATE_PATH) as f:
                pct = int(f.read().strip())
        except Exception:
            pct = None
        source = "persisted"
        if pct is None or not 0 <= pct <= 100:
            pct = self._startup_volume
            source = "startup_volume"
        try:
            from hal.models import VolumeRequest
            from hal.routes.audio import set_volume

            set_volume(VolumeRequest(volume=pct))
            logger.info(
                "[media-owner] pollen release reset the mixer — restored %d%% (%s)", pct, source
            )
        except Exception as e:
            logger.warning(
                "[media-owner] could not restore volume %d%% after release: %s", pct, e
            )

    def release(self) -> bool:
        """Take the camera and audio devices from the daemon."""
        for attempt in range(1, self._RELEASE_ATTEMPTS + 1):
            try:
                ok = self._post("release")
                self._restore_volume()
                return ok
            except Exception as e:
                if attempt == self._RELEASE_ATTEMPTS:
                    logger.warning(
                        "[media-owner] pollen release failed after %d attempts (%s) — "
                        "camera and audio will report 'device busy'",
                        self._RELEASE_ATTEMPTS, e,
                    )
                    return False
                logger.info(
                    "[media-owner] pollen release attempt %d/%d failed (%s) — retrying in %.0fs",
                    attempt, self._RELEASE_ATTEMPTS, e, self._RETRY_DELAY_S,
                )
                time.sleep(self._RETRY_DELAY_S)
        return False

    def acquire(self) -> bool:
        """Give the camera and audio devices back to the daemon."""
        try:
            return self._post("acquire")
        except Exception as e:
            logger.warning(
                "[media-owner] pollen acquire failed (%s) — daemon stays without media", e
            )
            return False
