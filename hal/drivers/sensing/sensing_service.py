"""
Sensing Service — background loop that detects motion/sound/faces/light and pushes events to the OS server.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import requests

import hal.config as config
from hal.drivers.camera.video_capture_device import VideoCaptureDeviceBase
from hal.drivers.motors.animation_service import AnimationService
from hal.drivers.rgb.rgb_service import RGBService
from hal.drivers.sensing.perceptions.models import PerceptionConfig
from hal.drivers.sensing.perceptions.orchestrator import PerceptionOrchestrator
from hal.drivers.sensing.perceptions.processors.faceid.enter_message import (
    has_new_friend,
)
from hal.drivers.sensing.presence_service import PresenseService
from hal.drivers.voice.tts import TTSService

logger = logging.getLogger(__name__)

try:
    import cv2
except ImportError:
    if TYPE_CHECKING:
        import cv2
    else:
        cv2 = None


try:
    import numpy as np
except ImportError:
    if TYPE_CHECKING:
        import numpy as np
    else:
        np = None

try:
    import sounddevice as sd
except ImportError:
    if TYPE_CHECKING:
        import sounddevice as sd
    else:
        sd = None


class SensingService:
    """Background sensing loop. Runs in a daemon thread."""

    # Settle time after freezing servos before capturing a frame (seconds)
    FREEZE_SETTLE_S: float = 0.3

    def __init__(
        self,
        camera_capture: VideoCaptureDeviceBase | None = None,
        input_device: int | str | None = None,
        poll_interval: float = 2.0,
        rgb_service: RGBService | None = None,
        tts_service: TTSService | None = None,
        animation_service: AnimationService | None = None,
        is_sleeping: Callable[[], bool] | None = None,
        enable_people_perception: bool = True,
    ):
        self._camera: VideoCaptureDeviceBase | None = camera_capture
        self._input_device: int | str | None = input_device
        self._poll_interval: float = poll_interval
        self._rgb_service: RGBService | None = rgb_service
        self._tts_service: TTSService | None = tts_service
        self._animation_service: AnimationService | None = animation_service
        self._is_sleeping: Callable[[], bool] | None = (
            is_sleeping
        )

        self._running: bool = False
        self._thread: threading.Thread | None = None
        self._last_event_time: dict[str, float] = {}

        self._perception_orchestrator: PerceptionOrchestrator = PerceptionOrchestrator(
            poll_interval_ts=self._poll_interval,
            send_event=self._send_event,
            sound_device_id=self._input_device,
            perception_config=PerceptionConfig(
                # People perception (face identity + facial emotion) is the `presence`
                # capability — ML over the camera via perception-service.
                enable_face=enable_people_perception,
                enable_motion=enable_people_perception and config.MOTION_ENABLED,
                enable_motion_per_face=enable_people_perception and config.MOTION_PER_FACE_ENABLED,
                enable_emotion=enable_people_perception and config.EMOTION_ENABLED,
                # Pose/ergonomics reads the USER's body posture — people perception too
                # (the physical axis of `presence`, via the camera).
                enable_pose=enable_people_perception and config.POSE_ENABLED,
                enable_light=True,
                enable_sound=True,
                enable_fire_hazard=config.FIRE_HAZARD_ENABLED,
            ),
        )

        self._presense_service: PresenseService = PresenseService(
            rgb_service=rgb_service,
            send_event=self._send_event,
            auto_enabled=enable_people_perception,
        )
        _ = self._perception_orchestrator.with_presence_service(self._presense_service)

        if self._camera is not None:
            _ = self._perception_orchestrator.with_camera_service(self._camera)

        if self._tts_service is not None:
            _ = self._perception_orchestrator.with_tts_service(self._tts_service)

    def set_tts_service(self, tts_service: TTSService):
        """Set TTS reference after late initialization (echo suppression)."""
        _ = self._perception_orchestrator.with_tts_service(tts_service)

    @property
    def presence(self) -> PresenseService:
        return self._presense_service

    def start(self):
        if self._running:
            return
        self._running = True
        self._perception_orchestrator.start()

    def stop(self):
        self._running = False
        self._perception_orchestrator.stop()
        logger.info("SensingService stopped")

    def _capture_stable_frame(self):
        """Freeze servos, wait for settle, capture a fresh frame, then unfreeze."""
        if not self._camera or not cv2:
            return None

        anim = self._animation_service
        if anim:
            anim.freeze()
            time.sleep(self.FREEZE_SETTLE_S)
        frame = self._camera.capture()
        if anim:
            anim.unfreeze()

        if frame is None:
            return None
        return frame.frame

    _snapshot_tmp_paths: list[str] = []

    def _save_frame(self, prefix: str, frame: cv2.typing.MatLike) -> str | None:
        """Save a camera frame as a JPEG to the tmp snapshot dir at original resolution."""
        try:
            os.makedirs(config.SNAPSHOT_TMP_DIR, exist_ok=True)
            tmp_dir = Path(config.SNAPSHOT_TMP_DIR)

            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])

            filename = f"{int(time.time() * 1000)}.jpg"
            subdir = tmp_dir / f"sensing_{prefix}"
            subdir.mkdir(exist_ok=True, parents=True)
            filepath = subdir / filename
            with open(filepath, "wb") as f:
                _ = f.write(buf.tobytes())

            self._snapshot_tmp_paths.append(str(filepath))

            while len(self._snapshot_tmp_paths) > config.SNAPSHOT_TMP_MAX_COUNT:
                oldest = self._snapshot_tmp_paths.pop(0)
                try:
                    os.remove(oldest)
                except OSError:
                    pass

            return str(filepath)
        except Exception as e:
            logger.debug("Frame save failed: %s", e)
            return None

    def _persist_snapshot(self, prefix: str, tmp_path: str) -> str | None:
        """Copy a tmp snapshot to the persistent dir with TTL + size rotation."""
        try:
            persist_dir = os.path.join(config.SNAPSHOT_PERSIST_DIR, f"sensing_{prefix}")
            os.makedirs(persist_dir, exist_ok=True)

            now = time.time()
            for f in os.listdir(persist_dir):
                fp = os.path.join(persist_dir, f)
                try:
                    if now - os.path.getmtime(fp) > config.SNAPSHOT_PERSIST_TTL_S:
                        os.remove(fp)
                except OSError:
                    pass

            files = []
            for f in os.listdir(persist_dir):
                fp = os.path.join(persist_dir, f)
                try:
                    files.append((fp, os.path.getmtime(fp), os.path.getsize(fp)))
                except OSError:
                    pass
            files.sort(key=lambda x: x[1])
            total = sum(s for _, _, s in files)
            while total > config.SNAPSHOT_PERSIST_MAX_BYTES and files:
                oldest_path, _, oldest_size = files.pop(0)
                try:
                    os.remove(oldest_path)
                    total -= oldest_size
                except OSError:
                    pass

            dest = os.path.join(persist_dir, os.path.basename(tmp_path))

            _ = shutil.copy2(tmp_path, dest)
            return dest
        except Exception as e:
            logger.debug("Persist snapshot failed: %s", e)
            return None

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        last_events = {k: int(now - v) for k, v in self._last_event_time.items()}
        return {
            "running": self._running,
            "poll_interval": self._poll_interval,
            "last_event_seconds_ago": last_events,
            "perceptions": self._perception_orchestrator.perceptions_state(),
            "presence": self._presense_service.to_dict(),
        }

    def _send_event(
        self,
        event_type: str,
        message: str,
        prefix: str = "",
        images: list[cv2.typing.MatLike] | None = None,
        cooldown: float | None = None,
    ):
        if self._is_sleeping and self._is_sleeping() and event_type != "presence.enter":
            logger.debug("[sensing] sleeping — suppressed %s", event_type)
            return

        if event_type == "presence.enter":
            self._perception_orchestrator.reset_dedup()

        cur_ts = time.time()
        # motion.activity, emotion.detected, fire_hazard.detected, and sound have their
        # own dedup logic — skip the global cooldown so events are never silently
        # dropped.
        if event_type not in ("motion.activity", "emotion.detected", "fire_hazard.detected", "sound"):
            cd = cooldown if cooldown is not None else config.EVENT_COOLDOWN_S
            last = self._last_event_time.get(event_type, 0)
            if cur_ts - last < cd:
                return

        if event_type == "presence.enter":
            self._grant_wakeword_focus_for_presence(message)

        frames = images or []

        for frame in frames:
            tmp_path = self._save_frame(prefix, frame)
            if tmp_path:
                persist_path = self._persist_snapshot(prefix, tmp_path)
                ref = persist_path or tmp_path
                message = f"{message}\n[snapshot: {ref}]"

        logger.info("[sensing] %s: %s", event_type, message)

        payload: dict[str, object] = {"type": event_type, "message": message}
        try:
            from hal import app_state as identity_state

            cu, _display, _source, _age = identity_state.resolve_current_user()
        except Exception:
            logger.exception("[sensing] current_user resolution failed")
            cu = ""
        payload["current_user"] = cu
        logger.debug("[sensing] payload = %s", payload)

        try:
            resp = requests.post(
                config.OS_SENSING_URL,
                json=payload,
                timeout=5,
            )
            if resp.status_code != 200:
                logger.warning(
                    "[sensing] OS server returned %d: %s", resp.status_code, resp.text
                )
            else:
                self._last_event_time[event_type] = cur_ts
        except requests.RequestException as e:
            logger.warning("[sensing] Failed to send event to the OS server: %s", e)

    @staticmethod
    def _grant_wakeword_focus_for_presence(message: str) -> None:
        """Let a newly recognized person stand in for the wake phrase."""
        if not has_new_friend(message) and not config.PRESENCE_WAKE_STRANGERS:
            logger.info("[sensing] stranger-only presence.enter — wake focus not granted")
            return
        try:
            from hal import app_state as state

            voice = state.voice_service
            if voice is not None and hasattr(voice, "grant_wakeword_focus"):
                voice.grant_wakeword_focus("presence.enter")
        except Exception as e:
            # Presence reporting must continue even when voice is unavailable.
            logger.warning("[sensing] presence.enter wake-focus grant failed: %s", e)
