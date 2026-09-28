"""Local-only sensing surface for HAL_SIMULATE."""

from __future__ import annotations

from hal.drivers.sensing.presence_service import PresenseService


class VirtualSensingService:
    def __init__(self, camera_capture=None, poll_interval: float = 2.0, rgb_service=None, **_):
        self._camera = camera_capture
        self._poll_interval = poll_interval
        self._running = False
        self._presense_service = PresenseService(rgb_service=rgb_service, auto_enabled=True)

    @property
    def presence(self):
        return self._presense_service

    def start(self):
        self._running = True

    def stop(self):
        self._running = False

    def to_dict(self):
        return {
            "running": self._running,
            "poll_interval": self._poll_interval,
            "last_event_seconds_ago": {},
            "perceptions": [],
            "presence": self._presense_service.to_dict(),
        }
