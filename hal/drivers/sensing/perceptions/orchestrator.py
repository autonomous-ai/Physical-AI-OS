import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import hal.app_state as app_state
from hal.drivers.camera.video_capture_device import VideoCaptureDeviceBase
from hal.drivers.sensing.perceptions.models import (
    PerceptionConfig,
)
from hal.drivers.sensing.perceptions.processors import (
    EmotionPerception,
    FacePerception,
    LightLevelPerception,
    MotionPerception,
    MotionPerFacePerception,
    PosePerception,
    SoundPerception,
)
from hal.drivers.sensing.perceptions.processors.base import Perception
from hal.drivers.sensing.perceptions.processors.fire_hazard import FireHazardPerception
from hal.drivers.sensing.perceptions.typing import SendEventCallable
from hal.drivers.sensing.perceptions.utils import PerceptionStateObservers
from hal.drivers.sensing.presence_service import PresenseService
from hal.drivers.voice.tts import TTSService

try:
    import cv2
except ImportError:
    cv2 = None


try:
    import numpy as np
except ImportError:
    np = None

try:
    import sounddevice as sd
except ImportError:
    sd = None


@dataclass
class PerceptionProcessors:
    face_recognizer: FacePerception | None = None
    motion_processor: MotionPerception | None = None
    motion_per_face_processor: MotionPerFacePerception | None = None
    emotion_processor: EmotionPerception | None = None
    pose_processor: PosePerception | None = None
    light_processor: LightLevelPerception | None = None
    sound_recognizer: SoundPerception | None = None
    fire_hazard_processor: FireHazardPerception | None = None

    def processors_dict(self) -> dict[str, Perception]:
        result: dict[str, Perception] = {}
        if self.face_recognizer is not None:
            result["face"] = self.face_recognizer
        if self.motion_processor is not None:
            result["motion"] = self.motion_processor
        if self.motion_per_face_processor is not None:
            result["motion_per_face"] = self.motion_per_face_processor
        if self.emotion_processor is not None:
            result["emotion"] = self.emotion_processor
        if self.pose_processor is not None:
            result["pose"] = self.pose_processor
        if self.light_processor is not None:
            result["light"] = self.light_processor
        if self.sound_recognizer is not None:
            result["sound"] = self.sound_recognizer
        if self.fire_hazard_processor is not None:
            result["fire_hazard"] = self.fire_hazard_processor
        return result


class PerceptionOrchestrator:
    def __init__(
        self,
        poll_interval_ts: float,
        send_event: SendEventCallable,
        perception_config: PerceptionConfig | None = None,
        sound_device_id: int | str | None = None,
    ):
        self._poll_interval_ts: float = poll_interval_ts
        self._send_event: SendEventCallable = send_event
        self._config: PerceptionConfig = (
            perception_config if perception_config is not None else PerceptionConfig()
        )
        self._sound_device_id: int | str | None = sound_device_id

        self._camera_capture: VideoCaptureDeviceBase | None = None
        self._presense_service: PresenseService | None = None
        self._tts_service: TTSService | None = None

        self._stopped: threading.Event = threading.Event()
        self._main_loop_thread: threading.Thread | None = None
        self._processors: PerceptionProcessors = PerceptionProcessors()
        self._perception_state: PerceptionStateObservers = PerceptionStateObservers()

        self._logger: logging.Logger = logging.getLogger(self.__class__.__name__)

    def with_camera_service(self, camera_capture: VideoCaptureDeviceBase):
        self._camera_capture = camera_capture
        return self

    def with_presence_service(self, presence_service: PresenseService):
        self._presense_service = presence_service
        return self

    def with_tts_service(self, tts_service: TTSService):
        self._tts_service = tts_service

        if self._processors.sound_recognizer is not None:
            self._processors.sound_recognizer.set_tts_service(tts_service)

        return self

    def _register_processors(self):
        if cv2 is not None:
            if self._config.enable_face:
                self._processors.face_recognizer = FacePerception(
                    perception_state=self._perception_state,
                    send_event=self._send_event,
                    presense_service=self._presense_service,
                )
                _ = self._processors.face_recognizer.load_from_disk()
                self._perception_state.frame.register(
                    self._processors.face_recognizer.check
                )

            if self._config.enable_motion:
                self._processors.motion_processor = MotionPerception(
                    perception_state=self._perception_state,
                    send_event=self._send_event,
                    presense_service=self._presense_service,
                )
                self._perception_state.frame.register(
                    self._processors.motion_processor.check
                )

            if self._config.enable_motion_per_face:
                self._processors.motion_per_face_processor = MotionPerFacePerception(
                    perception_state=self._perception_state,
                    send_event=self._send_event,
                    presense_service=self._presense_service,
                )
                self._perception_state.detected_faces.register(
                    self._processors.motion_per_face_processor.check
                )

            if self._config.enable_emotion:
                self._processors.emotion_processor = EmotionPerception(
                    perception_state=self._perception_state,
                    send_event=self._send_event,
                    presense_service=self._presense_service,
                )
                self._perception_state.detected_faces.register(
                    self._processors.emotion_processor.check
                )

            if self._config.enable_pose:
                self._processors.pose_processor = PosePerception(
                    perception_state=self._perception_state,
                    send_event=self._send_event,
                    presense_service=self._presense_service,
                )
                self._perception_state.frame.register(
                    self._processors.pose_processor.check
                )
                if self._processors.motion_processor is not None:
                    self._processors.motion_processor.set_pose_perception(
                        self._processors.pose_processor
                    )

            if self._config.enable_light:
                self._processors.light_processor = LightLevelPerception(
                    perception_state=self._perception_state,
                    send_event=self._send_event,
                )
                self._perception_state.frame.register(
                    self._processors.light_processor.check
                )

            if self._config.enable_fire_hazard:
                self._processors.fire_hazard_processor = FireHazardPerception(
                    perception_state=self._perception_state,
                    send_event=self._send_event,
                    presense_service=self._presense_service,
                )
                self._perception_state.frame.register(
                    self._processors.fire_hazard_processor.check
                )

        if (
            self._config.enable_sound
            and sd is not None
            and np is not None
            and self._sound_device_id is not None
        ):
            self._processors.sound_recognizer = SoundPerception(
                sd=sd,
                np_module=np,
                perception_state=self._perception_state,
                send_event=self._send_event,
                input_device=self._sound_device_id,
                tts_service=self._tts_service,
            )
            self._perception_state.frame.register(
                self._processors.sound_recognizer.check
            )
        elif self._config.enable_sound:
            self._logger.warning(
                "SoundPerception disabled: sd=%s np=%s sound_device_id=%s",
                sd is not None,
                np is not None,
                self._sound_device_id,
            )

    def start(self):
        if self._main_loop_thread is not None:
            self._logger.info(
                "[%s] service has been already started", self.__class__.__name__
            )
            return
        self._register_processors()
        self._main_loop_thread = threading.Thread(
            target=self._loop, daemon=True, name="sensing"
        )
        self._main_loop_thread.start()
        self._logger.info("SensingService started (poll=%.1fs)", self._poll_interval_ts)

    def stop(self):
        self._stopped.set()
        if self._main_loop_thread:
            self._main_loop_thread.join(timeout=5)
            self._main_loop_thread = None

        for name, p in self._processors.processors_dict().items():
            try:
                p.cleanup()
            except Exception:
                self._logger.exception("[%s] %s.cleanup() failed", self.__class__.__name__, name)

        self._logger.info("SensingService stopped")

    def _loop(self):
        # Wait a bit for hardware to initialize
        time.sleep(3)

        while not self._stopped.is_set():
            try:
                self._tick()
            except Exception as e:
                self._logger.exception("Sensing tick error: %s", e)

            time.sleep(self._poll_interval_ts)

    def _tick(self):
        # Read camera frame once per tick (shared across detectors). try/except kept as
        # a safety net in case the flag is out of sync with the device thread state.
        if self._camera_capture and not app_state._camera_disabled:
            try:
                response = self._camera_capture.capture()
            except RuntimeError:
                response = None
            if response is not None and response.frame is not None:
                self._perception_state.frame.data = response.frame

        if self._presense_service is not None:
            self._presense_service.tick()

    def perceptions_state(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self._processors.processors_dict().values()]

    def reset_dedup(self):
        new_user = self._perception_state.current_user.data or ""
        for name, p in self._processors.processors_dict().items():
            reset = getattr(p, "reset_dedup", None)
            if callable(reset):
                try:
                    reset(new_user)
                except Exception:
                    self._logger.exception("[%s] %s.reset_dedup() failed", self.__class__.__name__, name)

    @property
    def current_user(self):
        return self._perception_state.current_user.data
