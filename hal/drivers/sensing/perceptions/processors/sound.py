import logging
import threading
import time
from typing import Any, override

import requests

import hal.config as config
from hal.drivers.sensing.perceptions.typing import SendEventCallable
from hal.drivers.sensing.perceptions.utils import PerceptionStateObservers

from .base import Perception

_MONITOR_URL = "http://127.0.0.1:5000/api/monitor/event"

logger = logging.getLogger(__name__)

_DEDUPE_INTERVAL_S = 15.0
_WINDOW_DURATION_S = 120.0
_PERSISTENT_AFTER = 3
_SUPPRESS_DURATION_S = 180.0
# Conversation guard window: skip loud-noise events this long after STT last produced
# transcript text.
_CONVERSATION_HOLDOFF_S = 15.0
_READING_MAX_AGE_S = 10.0


class SoundPerception(Perception[Any]):
    """Detects loud sounds via microphone RMS energy."""

    def __init__(
        self,
        sd,
        np_module,
        perception_state: PerceptionStateObservers,
        send_event: SendEventCallable,
        input_device=None,  # int (sounddevice index) or str (ALSA device name)
        tts_service=None,
    ):
        super().__init__(perception_state, send_event)
        self._sd = sd
        self._np = np_module
        # Resolve ALSA device name to sounddevice index if needed
        if isinstance(input_device, str):
            # Strip ALSA plugin prefix (e.g. "plug:device_micro1" → "device_micro1")
            search_name = (
                input_device.split(":")[-1] if ":" in input_device else input_device
            )
            resolved = None
            devices = sd.query_devices()
            for i, info in enumerate(devices):
                logging.getLogger(__name__).info(
                    "  device %d: name='%s' inputs=%d",
                    i,
                    info["name"],
                    info.get("max_input_channels", 0),
                )
                if info["name"] == search_name or info["name"].startswith(
                    search_name + ","
                ):
                    resolved = i
                    break
            if resolved is not None:
                logging.getLogger(__name__).info(
                    "Resolved sensing device '%s' → index %d", input_device, resolved
                )
                input_device = resolved
            else:
                logging.getLogger(__name__).warning(
                    "Could not resolve sensing device '%s', falling back to default",
                    input_device,
                )
                input_device = None
        self._input_device = input_device
        self._tts = tts_service

        self._count: int = 0
        self._window_start: float = 0.0
        self._last_passed: float = 0.0
        self._suppress_until: float = 0.0
        # Habituation: after one persistent (shock) reaction, stay quiet while the same
        # noise goes on. Cleared only by _WINDOW_DURATION_S of quiet.
        self._habituated: bool = False
        self._last_heard: float = 0.0
        self._last_rms: float = 0.0
        self._last_rms_ts: float = 0.0

        # Async capture. sd.rec(blocking=True) takes SOUND_SAMPLE_DURATION_S (0.5s) and
        # used to run INSIDE the sensing observer chain — every perception behind sound
        # waited out the recording on every tick.
        self._sample_req = threading.Event()
        self._capture_stop = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._reading: tuple[float, float] | None = None

    @property
    def last_level(self) -> tuple[float, float]:
        """(last sampled RMS on int16 scale, unix ts of that sample)."""
        return self._last_rms, self._last_rms_ts

    def set_tts_service(self, tts_service) -> None:
        self._tts = tts_service

    def _push_monitor(self, event_type: str, summary: str, detail: dict[str, Any]) -> None:
        def _send():
            try:
                requests.post(
                    _MONITOR_URL,
                    json={"type": event_type, "summary": summary, "detail": detail},
                    timeout=5,
                )
            except Exception:
                pass

        threading.Thread(target=_send, daemon=True).start()

    def _track(self, now: float) -> tuple[bool, int, bool]:
        """Returns (send, occurrence, persistent)."""
        if self._habituated:
            quiet_s = now - self._last_heard
            self._last_heard = now
            if quiet_s < _WINDOW_DURATION_S:
                return False, 0, False
            self._habituated = False
        if now < self._suppress_until:
            return False, 0, False

        if self._last_passed and (now - self._last_passed) > _WINDOW_DURATION_S:
            self._count = 0
            self._window_start = 0.0

        if self._last_passed and (now - self._last_passed) < _DEDUPE_INTERVAL_S:
            return False, 0, False

        if not self._window_start:
            self._window_start = now
        self._count += 1
        self._last_passed = now

        current = self._count
        persistent = current >= _PERSISTENT_AFTER
        if persistent:
            self._suppress_until = now + _SUPPRESS_DURATION_S
            self._habituated = True
            self._last_heard = now
            self._count = 0
            self._window_start = 0.0

        return True, current, persistent

    @override
    def cleanup(self) -> None:
        self._capture_stop.set()
        self._sample_req.set()

    def _ensure_capture_thread(self) -> None:
        if self._capture_thread is not None and self._capture_thread.is_alive():
            return
        self._capture_stop.clear()
        self._capture_thread = threading.Thread(
            target=self._capture_loop, daemon=True, name="sound-capture"
        )
        self._capture_thread.start()

    def _capture_loop(self) -> None:
        """Record one RMS sample per request from _check_impl (see __init__ note). The 0.5s
        blocking rec lands on THIS thread, not the sensing observer chain.
        """
        sample_rate = 44100
        frames = int(sample_rate * config.SOUND_SAMPLE_DURATION_S)
        while not self._capture_stop.is_set():
            if not self._sample_req.wait(timeout=0.5):
                continue
            if self._capture_stop.is_set():
                return
            self._sample_req.clear()
            try:
                recording = self._sd.rec(
                    frames,
                    samplerate=sample_rate,
                    channels=1,
                    dtype="int16",
                    device=self._input_device,
                    blocking=True,
                )
                rms = float(
                    self._np.sqrt(
                        self._np.mean(recording.astype(self._np.float64) ** 2)
                    )
                )
                self._reading = (rms, time.time())
            except Exception as e:
                logger.debug("Sound capture failed: %s", e)
                # Don't spin on a dead device — pause before honoring the
                # next request.
                self._capture_stop.wait(2.0)

    @override
    def _check_impl(self, data: Any) -> None:
        if self._input_device is None:
            return

        if self._tts is not None and (
            self._tts.speaking or time.time() - self._tts.last_spoken_time < 5.0
        ):
            self._sample_req.clear()  # don't record the tail of our own speech
            return

        try:
            import hal.app_state as app_state

            music = app_state.music_service
            if music is not None and music.streaming:
                return
            anim = app_state.animation_service
            if anim is not None and anim.is_actively_moving:
                return
            voice = app_state.voice_service
            if voice is not None and (
                time.time() - getattr(voice, "last_transcript_ts", 0.0)
            ) < _CONVERSATION_HOLDOFF_S:
                return
        except Exception:
            pass

        try:
            self._ensure_capture_thread()
            reading = self._reading
            self._reading = None
            self._sample_req.set()
            if reading is None:
                return
            rms, ts = reading
            if time.time() - ts > _READING_MAX_AGE_S:
                return
            self._last_rms = rms
            self._last_rms_ts = ts
            if rms < config.SOUND_RMS_THRESHOLD:
                return

            now = time.time()
            send, occurrence, persistent = self._track(now)
            if not send:
                self._push_monitor(
                    "sound_tracker",
                    "sound dropped (dedup/suppressed)",
                    {"action": "drop"},
                )
                return

            msg = f"Loud noise detected (level: {int(rms)})"
            if persistent:
                msg += f" — persistent (occurrence {occurrence})"
                self._push_monitor(
                    "sound_tracker",
                    f"sound persistent — occurrence {occurrence} → will speak",
                    {"action": "persistent", "occurrence": occurrence},
                )
            elif occurrence == 1:
                msg += f" — occurrence {occurrence}"
                self._push_monitor(
                    "sound_tracker",
                    f"sound occurrence {occurrence} → silent",
                    {"action": "silent", "occurrence": occurrence},
                )
            else:
                self._push_monitor(
                    "sound_tracker",
                    f"sound occurrence {occurrence} → counted, not forwarded",
                    {"action": "counted", "occurrence": occurrence},
                )
                return

            self._send_event("sound", msg, "", None, None)
        except Exception as e:
            logger.debug("Sound check failed: %s", e)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "sound",
            "input_device": self._input_device,
            "echo_suppression": self._tts is not None,
            "occurrence_count": self._count,
            "suppressed": time.time() < self._suppress_until,
            "last_rms": int(self._last_rms),
        }
