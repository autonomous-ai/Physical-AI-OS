"""Realtime execution on the device FIFO finalizer, independent of mic capture."""

import logging
import time

from hal import config
from hal.drivers.voice._internal import config as voice_config
from hal.drivers.voice._internal.realtime_turn import (
    ROUTE_CANCELLED, ROUTE_ERROR, ROUTE_NOISE_DROPPED, ROUTE_UNAVAILABLE,
    RealtimeTurnResult, build_turn_context, is_noise_turn, run_realtime_turn,
    should_drop_downstream_turn,
)
from hal.realtime.utils import StreamingResampler, pcm16_bytes_to_float32


logger = logging.getLogger("hal.voice")


class _DeviceOutput:
    """Use current TTS for admission; pin native frames to the admitted owner."""

    def __init__(self, get_tts, cancelled, valid, *, timeout=30.0):
        self._get_tts = get_tts
        self._cancelled = cancelled
        self._valid = valid
        self._timeout = timeout
        self._native_owner = None

    def __getattr__(self, name):
        return getattr(self._get_tts(), name)

    def _turn_valid(self):
        if self._cancelled.is_set():
            return False
        if not self._valid():
            self._cancelled.set()
            return False
        return True

    def speak(self, *args, **kwargs):
        tts = self._get_tts()
        return bool(tts is not None and tts.speak(
            *args, **kwargs, _device_turn_valid=self._turn_valid,
        ))

    def speak_queue(self, *args, **kwargs):
        tts = self._get_tts()
        return bool(tts is not None and tts.speak_queue(
            *args, **kwargs, _device_turn_valid=self._turn_valid,
        ))

    def native_play_begin(self, rate, owner=""):
        deadline = time.monotonic() + self._timeout
        while not self._cancelled.is_set():
            if not self._valid():
                self._cancelled.set()
                return False
            tts = self._get_tts()
            if tts is None:
                return False
            capturing = getattr(tts, "input_capture_state", (False, 0))[0]
            if not capturing:
                if tts.native_play_begin(rate, owner=owner):
                    self._native_owner = tts
                    return True
                # A new capture can win between the state read and admission.
                if not getattr(tts, "input_capture_state", (False, 0))[0]:
                    return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("device capture did not release native speech admission")
            self._cancelled.wait(min(0.02, remaining))
        return False

    def native_play_frame(self, frame):
        if self._cancelled.is_set() or self._native_owner is None:
            return False
        return self._native_owner.native_play_frame(frame)

    def native_play_end(self, transcript=""):
        owner, self._native_owner = self._native_owner, None
        if owner is not None:
            owner.native_play_end(transcript)

    def stop_realtime_reply(self, *, turn_id=""):
        current = self._get_tts()
        if self._native_owner is not None:
            self._native_owner.stop_realtime_reply(turn_id=turn_id)
        if current is not None and current is not self._native_owner:
            current.stop_realtime_reply(turn_id=turn_id)


class DeviceRealtimeTurn:
    """One callable per finalized turn; the caller supplies FIFO serialization."""

    def __init__(self, *, realtime, tts, strip_markers):
        self._get_realtime = realtime
        self._get_tts = tts
        self._strip_markers = strip_markers

    def __call__(self, audio, combined, duration, speech, interaction_id,
                 speaker, cancelled, valid):
        result = self._execute(audio, combined, duration, speech, interaction_id,
                               speaker, cancelled, valid)
        if cancelled.is_set() or not valid():
            cancelled.set()
            return RealtimeTurnResult(route=ROUTE_CANCELLED)
        if combined and not result.handled and not should_drop_downstream_turn(result):
            realtime = self._get_realtime()
            if realtime is not None:
                try:
                    # Memory belongs to the orchestrator, not its current
                    # connection; unavailable-provider fallbacks still count.
                    realtime.save_main_handoff(combined)
                except Exception:
                    logger.exception("Device main-agent handoff memory failed")
        return result

    def _execute(self, audio, combined, duration, speech, interaction_id,
                 speaker, cancelled, valid):
        def active():
            if cancelled.is_set():
                return False
            if not valid():
                cancelled.set()
                return False
            return True

        if not active():
            return RealtimeTurnResult(route=ROUTE_CANCELLED)
        if is_noise_turn(combined, duration, speech):
            return RealtimeTurnResult(route=ROUTE_NOISE_DROPPED)
        if not config.REALTIME_ENABLED or not audio:
            return RealtimeTurnResult()
        realtime = self._get_realtime()
        if realtime is None:
            return RealtimeTurnResult(route=ROUTE_UNAVAILABLE)
        binding = None
        handed_off = False

        def discard():
            if binding is not None and not handed_off:
                try:
                    realtime.recover_session("device-turn-discarded", discard_old_on_failure=True)
                except Exception:
                    logger.exception("Device realtime cleanup failed")

        try:
            realtime.prepare_turn()
            if not active():
                return RealtimeTurnResult(route=ROUTE_CANCELLED)
            if not realtime.wait_until_available():
                return RealtimeTurnResult(route=ROUTE_UNAVAILABLE)
            if not active():
                return RealtimeTurnResult(route=ROUTE_CANCELLED)
            binding = realtime.bind_audio_turn()
            if not active():
                return RealtimeTurnResult(route=ROUTE_CANCELLED)
            realtime.send_text(build_turn_context(speaker))
            resampler = StreamingResampler(voice_config.STT_RATE, realtime.sample_rate)
            frames = []
            for pcm in audio:
                if not active():
                    return RealtimeTurnResult(route=ROUTE_CANCELLED)
                frame = resampler.process(pcm16_bytes_to_float32(pcm))
                frames.append(frame)
                realtime.append_audio(frame, turn=binding)
            if not active():
                return RealtimeTurnResult(route=ROUTE_CANCELLED)
            output = (_DeviceOutput(self._get_tts, cancelled, active)
                      if self._get_tts() is not None else None)
            result = run_realtime_turn(
                realtime, output, self._strip_markers, combined, frames, duration, speech,
                interaction_id=interaction_id, audio_turn=binding,
                stop_event=cancelled, harness_followup=False, suppress_visual_feedback=True,
            )
            handed_off = True
            if not active():
                return RealtimeTurnResult(route=ROUTE_CANCELLED)
            return result
        except Exception:
            logger.exception("Device realtime turn failed; retaining STT fallback")
            return RealtimeTurnResult(route=ROUTE_CANCELLED if not active() else ROUTE_ERROR)
        finally:
            discard()
            realtime.finish_capture()
