"""Voice Service — local VAD + pluggable STT for autonomous sensing."""

import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from contextlib import contextmanager
from difflib import SequenceMatcher
from typing import Optional

import requests

from hal import config as hal_config
from hal import cpu_affinity
from hal import presets
from hal.realtime.enums import AgentGateway
from hal.realtime.models import AudioOutput as RTAudioOutput
from hal.realtime.models import InterruptedOutput as RTInterruptedOutput
from hal.realtime.models.signal import DelegateSignal, EndCallSignal, RejectSignal
from hal.realtime.models.output import ExecutionOutput, UserSpeechOutput
from hal.realtime.models import TextOutput as RTTextOutput
from hal.realtime.models import TextSegmentEndOutput as RTTextSegmentEndOutput
from hal.realtime.orchestrator import (
    RealtimeOrchestrator, AudioTurnSessionChanged, PREWARM_JOIN_TIMEOUT_S,
)
from hal.realtime.utils import StreamingResampler, pcm16_bytes_to_float32, resample_float32
from hal.drivers.voice._internal import config as voice_cfg
from hal.drivers.voice._internal import live_playback
from hal.drivers.voice.tts.gemini import native_voice
from hal.drivers.voice._internal.live_gate import AdaptiveLiveGate
from hal.drivers.voice._internal.live_reply import LiveReplyGuard
from hal.drivers.voice._internal.audio_dsp import resample_to_stt, rms
from hal.drivers.voice._internal.audio_recorder import ArecordStream
from hal.drivers.voice._internal.noise_guard import accepts_speech_metrics
from hal.drivers.voice._internal.realtime_turn import (
    realtime_speech_text,
    realtime_visible_text,
    split_delivery_sentence,
    split_realtime_first_chunk,
    _WaitFiller,
    ROUTE_DELEGATED,
    ROUTE_NOISE_DROPPED, ROUTE_NOT_ADDRESSED,
    split_first_chunk,
    split_completed_prefix,
    SENTENCE_ENDS,
    ROUTE_NOT_STARTED,
    RealtimeTurnResult,
    build_speaker_correction,
    build_turn_context,
    is_noise_turn,
    harness_followup_active,
    needs_noise_guard,
    run_realtime_turn,
    should_drop_downstream_turn,
    should_arm_realtime_wait_filler,
    should_defer_speaker_id_prepass,
    should_dispatch_to_main,
)
from hal.drivers.voice._internal.device_input import DeviceInputLease, DeviceTapInput
from hal.drivers.voice._internal.device_turn_queue import DeviceTurnQueue
from hal.drivers.voice._internal.device_voice_pipeline import DeviceVoicePipeline
from hal.drivers.voice._internal.device_realtime import DeviceRealtimeTurn
from hal.drivers.voice._internal.harness_capture import HarnessCapture
from hal.drivers.voice._internal.input_policy import (
    InputPolicy,
    device_manual_mode, requires_manual_capture, same_capture_target,
)
from hal.drivers.voice._internal.harness_voice import bypass_realtime, read_voice_mode
from hal.drivers.voice._internal.main_followup import note_main_reply
from hal.drivers.voice._internal.sensing_sender import SensingSender
from hal.drivers.voice._internal.session_finalize import finalize_session, recent_spoken_text
from hal.drivers.voice._internal.turn_admission import (
    addressed_hint, confident_partial, facing_evidence, register_tts, short_answer_expected,
    strict_addressed_gate,
)
from hal.drivers.voice._internal.speaker_decorate import (
    SpeakerDecorator,
    merge_stt_hypothesis,
    merge_wake_words,
)
from hal.drivers.voice._internal.turn_dispatch import dispatch_turn
from hal.drivers.voice._internal.turn_endpoint import TurnEndpoint
from hal.drivers.voice._internal.smart_turn import SmartTurnDetector
from hal.telemetry import voice_metrics
from hal.telemetry.live_voice import LiveVoiceMetrics
from hal.drivers.voice._internal.live_history import LiveHistory
from hal.drivers.voice._internal.live_cues import LiveVoiceCues
from hal.drivers.voice._internal.vad_filters import (
    SileroVADFilter,
    WebRTCVADFilter,
    turn_should_close,
)
from hal.drivers.voice._internal.wakeword_focus import WakeWordFocus, is_addressed
from hal.drivers.voice import aec
from hal.drivers.voice.backchannel import Backchannel
from hal.drivers.voice.stt import STTProvider

logger = logging.getLogger("hal.voice")

_TRANSCRIPT_MIN_SIMILARITY = 0.5


def _is_normal_ws_close(error: Exception) -> bool:
    """Whether an STT exception represents a peer's normal WS close (1000)."""
    code = getattr(error, "code", None)
    received = getattr(error, "rcvd", None)
    if code is None and received is not None:
        code = getattr(received, "code", None)
    return code == 1000 or "received 1000 (OK)" in str(error)


class VoiceService:
    """Local VAD + pluggable STT provider for autonomous sensing."""

    RT_MARKER_RE: re.Pattern[str] = re.compile(
        r"\[HW:/[^{\]]*(?:\{[^}]*\})?\]"
        r"|\[(?:laughs|LAUGHS|sighs|chuckle|light chuckle|giggle|big laugh|gasps|gulps|breathes|clears throat|whispers|pause|pauses|hesitates|stammers|thinking|thinks|thought|thoughtful|pondering|ponders|reasoning)"
        r"[^\]]*\]"
        r"|\[(?:cheerfully|playfully|quietly|nervously|deadpan|flatly|dramatic tone|resigned tone|excited|calm|tired|sad|sorrowful|nervous|frustrated)"
        r"[^\]]*\]"
        r"|`\[[^\]]*\]`"
        r"|/(?:emotion|servo|led|skills)[^\s]*"
        # Bare emotion-annotation prefix the realtime model sometimes emits and then
        # mimics from its own saved history (e.g. "emotion_user:concentration
        # intensity:1.0 emotion_model:calm intensity:1.0 …").
        r"|emotion_(?:user|model)\s*:\s*\S+"
        r"|\bintensity\s*:\s*[0-9.]+"
        r"|NO_REPLY",
        re.IGNORECASE,
    )

    # Markdown-link-form HW marker like [Lights off](HW:/led/off:{}) — some LLMs wrap
    # the marker in a link.
    RT_HW_LINK_RE: re.Pattern[str] = re.compile(
        r"\[([^\]]*)\]\(\s*HW:\s*(?:/[^(){:\s]+(?::[^(){:\s]+)*)(?::\{[^}]*\})?:?\s*\)",
        re.IGNORECASE,
    )

    @staticmethod
    def strip_rt_markers(text: str, *, preserve_audio_tags: bool = False) -> str:
        """Remove control markers; optionally retain delivery tags for ElevenLabs."""
        text = VoiceService.RT_HW_LINK_RE.sub(
            lambda m: "" if m.group(1)[:3].lower() == "hw:" else m.group(1), text
        )
        def replace_marker(match):
            marker = match.group(0)
            if (preserve_audio_tags and marker.startswith("[")
                    and not re.match(r"\[(?:HW:|thinking|thinks|thought|pondering|ponders|reasoning)",
                                     marker, re.IGNORECASE)):
                return marker
            return ""

        cleaned: str = VoiceService.RT_MARKER_RE.sub(replace_marker, text)
        cleaned = re.sub(r"  +", " ", cleaned).strip()
        cleaned = re.sub(r"^(?:(?:<\s*no\s+speech\s*>|\{\s*pause\s*\})\s*)+",
                         "", cleaned, flags=re.IGNORECASE)
        if VoiceService._pending_rt_silence_marker(cleaned):
            return ""
        return cleaned

    @staticmethod
    def _pending_rt_silence_marker(text: str) -> bool:
        partial = " ".join(text.lower().split())
        partial = re.sub(r"^\{\s*", "{", partial)
        return bool(partial) and any(
            marker.startswith(partial) for marker in ("<no speech>", "{pause}")
        )

    def __init__(
        self,
        stt_provider: STTProvider,
        input_device: Optional[int] = None,
        tts_service=None,
        music_service=None,
        wake_words: Optional[list] = None,
        alsa_device: Optional[str] = None,
        enable_people_perception: bool = True,
        enable_expression: bool = False,
    ):
        self._harness_capture = HarnessCapture(target_matches=same_capture_target)
        self._device_turn_queue = DeviceTurnQueue()
        self._device_dispose_thread = None
        self.device_input = DeviceTapInput(
            self._harness_capture, self.start_harness_capture,
            turn_queue=self._device_turn_queue,
            begin_input=lambda: DeviceInputLease.begin(self._tts),
            end_input=lambda lease: lease.close(),
        )
        self._stt = stt_provider
        self._input_device = input_device
        self._lifecycle_revision = 0
        self._lifecycle_lock = threading.Lock()
        self._mic_lock = threading.Lock()
        self._active_mic = None
        self._realtime_stop_thread = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._listening = False
        self._live_gate = AdaptiveLiveGate() if live_playback.ENABLED else None
        self._aec_live_replies = LiveReplyGuard() if getattr(self, "_live_gate", None) is not None else None
        self._aec_live_diag_next = 0.0
        self._aec_live_last_upload_ms = 0.0
        # Live (full-duplex) session state — see _live_session. The generation counter
        # exists because the output pump can still be blocked inside receive() when its
        # session ends and join() gives up before that.
        self._live_running = False
        self._live_generation = 0
        self._live_last_model_output = 0.0
        self._live_frames = 0
        self._live_frames_during_playback = 0
        self._live_frames_substituted = 0
        self._live_playback_was_speaking = False
        self._live_playback_tail_until = 0.0
        self._live_unprompted_replies = 0
        self._live_last_transcript_at = 0.0
        self._live_hangup_at = 0.0
        # Latest mic frame RMS (int16 scale) + capture timestamp — published by
        # the capture loops below, read by GET /voice/mic-level for the web VU
        # meter. Plain float writes are atomic under the GIL, no lock needed.
        self._mic_level = 0.0
        self._mic_level_ts = 0.0
        # When STT last produced transcript text (partial or final) — proof that the
        # loud audio in the room is PEOPLE TALKING, not noise.
        self._last_transcript_ts = 0.0
        self._tts = tts_service
        register_tts(tts_service)
        self._music = music_service
        self._device_rate: Optional[int] = None

        self._sd = None
        self._np = None
        self._alsa_device: Optional[str] = alsa_device or None

        self._backchannel = Backchannel(tts_service)

        try:
            import numpy as np

            self._np = np
        except ImportError:
            logger.warning("numpy not available for voice")

        try:
            import sounddevice as sd

            self._sd = sd
        except ImportError:
            logger.warning("sounddevice not available")

        # WebRTC VAD — fast C-based pre-filter (~0.1ms vs Silero ~20ms).
        # Enable via HAL_WEBRTCVAD_ENABLED=true in .env.
        self._webrtc_vad = (
            WebRTCVADFilter(voice_cfg.WEBRTCVAD_AGGRESSIVENESS, self._np)
            if voice_cfg.WEBRTCVAD_ENABLED
            else None
        )
        if not voice_cfg.WEBRTCVAD_ENABLED:
            logger.info("WebRTC VAD disabled (HAL_WEBRTCVAD_ENABLED=false)")

        self._silero_vad = (
            SileroVADFilter(voice_cfg.SILERO_MODEL_PATH, self._np) if voice_cfg.SILERO_VAD_ENABLED else None
        )
        if not voice_cfg.SILERO_VAD_ENABLED:
            logger.info("Silero VAD disabled via HAL_SILERO_ENABLED=false")
        # Dedicated Silero instance for the realtime empty-STT noise guard, built lazily
        # on first use.
        self._rt_noise_vad: SileroVADFilter | None = None
        self._silence_vad: SileroVADFilter | None = None
        self._turn_detector = None
        self._automatic_reply_lock = threading.Lock()
        self._automatic_reply_stop = None
        self._automatic_reply_cancelled_at = None
        self._stt_drain_worker = None
        self._stt_drain_future = None

        # Speaker decoration (wake-word + speaker recognizer + SER). Runtime rename
        # updates must never replace the permanent aliases.
        self._device_wake_words = list(voice_cfg.DEFAULT_WAKE_WORDS)
        self._wakeword_focus = WakeWordFocus(
            hal_config.WAKEWORD_FOLLOWUP_TIMEOUT_S,
            pending_speech=getattr(tts_service, "has_followup_speech", None),
        )

        # OS server event sender (with echo similarity filter)
        self._sensing_sender = SensingSender(tts_service=tts_service)

        self._realtime = RealtimeOrchestrator(
            gateway=AgentGateway(hal_config.AGENT_GATEWAY),
            enable_expression=enable_expression,
            stt_provider=stt_provider,
            voice_override=lambda: native_voice(tts_service),
        )

        if tts_service is not None:
            original_on_speak_end = tts_service._on_speak_end

            def _tts_speak_end_with_realtime_feedback() -> None:
                completion = tts_service.history_completion()
                self._wakeword_focus.playback_finished()
                if original_on_speak_end:
                    original_on_speak_end()
                if hal_config.REALTIME_ENABLED and completion is not None:
                    text, spoken, interrupted = completion
                    self.feed_realtime_history(text, spoken=spoken, interrupted=interrupted)

            tts_service._on_speak_end = _tts_speak_end_with_realtime_feedback

            # Same feed for a reply that never plays.
            def _unspoken_reply_to_realtime(text: str) -> None:
                self.feed_realtime_history(text, spoken=False)

            tts_service._on_unspoken_reply = _unspoken_reply_to_realtime

        self._decorator = SpeakerDecorator(
            wake_words=merge_wake_words(self._device_wake_words, wake_words or []),
            nudge_cooldown_s=voice_cfg.ENROLL_NUDGE_COOLDOWN_S,
            enable_people_perception=enable_people_perception,
        )
        self._device_pipeline = DeviceVoicePipeline(
            self._device_turn_queue, create_session=self._stt.create_session,
            convert=lambda data, rate: resample_to_stt(data, rate, voice_cfg.STT_RATE, self._np),
            valid=self._device_capture_valid, set_capturing=self._set_device_capturing,
            capture_valid=lambda snapshot: (self._device_capture_valid(snapshot)
                                            and not self._music_is_playing()),
            tts=self._tts, decorator=self._decorator, sensing_sender=self._sensing_sender,
            noise_is_speech=lambda pcm: self._rt_noise_is_speech(
                self._np.frombuffer(pcm, dtype=self._np.int16)),
            on_frame=self._device_mic_frame, on_transcript=self._device_transcript,
            stream_realtime=DeviceRealtimeTurn(
                realtime=lambda: self._realtime, tts=lambda: self._tts,
                strip_markers=self.strip_rt_markers,
            ).stream,
            record_handoff=lambda text: self._realtime.save_main_handoff(text),
        )
        if getattr(hal_config, "VOICE_INPUT_MODE", "automatic") == "tap_to_talk":
            # The pipeline is constructed even while muted/sleeping. Warm only
            # filter code now, so the first wake does not pay SciPy's cold import.
            aec.prepare_reference_background(voice_cfg.STT_RATE)

    def _device_capture_valid(self, snapshot):
        from hal import app_state

        return (self._running and not app_state._mic_muted
                and app_state._hw_mic_switch_muted is not True and not app_state._sleeping
                and same_capture_target(snapshot, read_voice_mode()))

    def replace_tts_service(self, factory):
        """Keep active device capture guarded while output service is replaced."""
        def install(tts):
            self._tts = tts
            register_tts(tts)
            self._device_pipeline.tts = tts
            if self._backchannel:
                self._backchannel._tts = tts

        return self.device_input.replace_input(factory, install)

    def _set_device_capturing(self, active):
        self._listening = active
        InputPolicy(True, True, False, False).set_capturing(active, self._set_emotion_local)

    def _device_mic_frame(self, data):
        self._mic_level = rms(data, self._np)
        self._mic_level_ts = time.time()
        return self._mic_level >= voice_cfg.RMS_THRESHOLD

    def _device_transcript(self, text, final):
        self._last_transcript_ts = time.time()
        logger.info("STT %s: %r", "final segment" if final else "partial", text)

    def feed_realtime_history(self, text: str, spoken: bool = True,
                              interrupted: bool = False) -> bool:
        """Give the realtime agent a main-agent reply it must stay aware of."""
        if not hal_config.REALTIME_ENABLED or not text:
            return False
        note_main_reply(text, heard=spoken or interrupted)
        self._realtime.save_main_agent_reply_fragment(text)
        max_hist = hal_config.REALTIME_TTS_HISTORY_MAX_CHARS
        if len(text) > max_hist:
            text = text[:max_hist] + "…"
        marker = "TTS HISTORY" if spoken else "TTS HISTORY, not spoken"
        if interrupted:
            marker = "TTS HISTORY, interrupted; only part may have been heard"
        logger.info(
            "[realtime<-tts] Notifying realtime agent (spoken=%s, interrupted=%s): %r",
            spoken,
            interrupted,
            text[:100],
        )
        self._realtime.send_text(f"[{marker}] {text}")
        return True

    def set_music_service(self, music_service) -> None:
        self._music = music_service

    def conversation_focus_active(self) -> bool:
        """Whether a wake-word follow-up window is currently open."""
        return bool(
            hal_config.WAKEWORD_ENABLED and self._wakeword_focus.is_active()
        )

    def grant_wakeword_focus(self, source: str = "button",
                             timeout_s: float | None = None) -> bool:
        """Open the wake-word follow-up window without a spoken wake phrase."""
        if not hal_config.WAKEWORD_ENABLED:
            return False
        if self._wakeword_focus.refresh(timeout_s):
            logger.info(
                "%s -- wake-word focus granted for %.0fs",
                source,
                hal_config.WAKEWORD_FOLLOWUP_TIMEOUT_S
                if timeout_s is None
                else min(hal_config.WAKEWORD_FOLLOWUP_TIMEOUT_S, timeout_s),
            )
            return True
        return False

    def followup_activity(self, interaction_id: str, run_id: str, phase: str) -> bool:
        """Track only main runs bound to a locally authorized wake turn."""
        if not hal_config.WAKEWORD_ENABLED:
            return False
        return self._wakeword_focus.activity(interaction_id, run_id, phase)

    def set_wake_words(self, words: list) -> None:
        """Update wake word list at runtime (called when agent is renamed)."""
        self._decorator.set_wake_words(
            merge_wake_words(self._device_wake_words, words)
        )

    @staticmethod
    def _set_emotion_local(emotion: str) -> None:
        """Set a device emotion by calling the HAL handler in-process."""
        try:
            from hal.models import EmotionRequest
            from hal.routes.emotion import express_emotion

            express_emotion(EmotionRequest(emotion=emotion))
        except Exception as e:
            logger.warning("emotion '%s' trigger failed: %s", emotion, e)

    @property
    def available(self) -> bool:
        return self._sd is not None and self._np is not None and self._stt.available

    @property
    def live_active(self) -> bool:
        """Whether the full-duplex microphone session is open."""
        return self._live_running

    @property
    def live_speaker_busy(self) -> bool:
        """LIVE is actually playing a reply, not merely keeping the mic open."""
        return bool(self.live_active and self._tts is not None
                    and self._tts.realtime_speaking)

    @property
    def listening(self) -> bool:
        return self._listening

    @property
    def last_transcript_ts(self) -> float:
        """Unix ts of the last non-empty STT transcript (partial or final)."""
        return self._last_transcript_ts

    @property
    def vad_threshold(self) -> float:
        if getattr(self, "_live_gate", None) is not None:
            return self._live_gate.threshold * 32768.0
        return voice_cfg.RMS_THRESHOLD

    @property
    def mic_level(self) -> float:
        """Latest mic input RMS (int16 scale, 0..32768)."""
        if (time.time() - self._mic_level_ts) > 1.0:
            return 0.0
        return self._mic_level

    def set_input_mode(self, mode, wakeword):
        from hal.drivers.voice._internal.input_mode import apply_to_service

        apply_to_service(self, mode, wakeword)

    def start(self):
        with self._lifecycle_lock:
            self._lifecycle_revision += 1
            self._input_mode_resume_pending = False
            self._start_locked()

    def _start_locked(self):
        # A timed-out teardown must retain ownership until its workers exit.
        if not self._running and (
            (self._thread is not None and self._thread.is_alive())
            or (self._realtime_stop_thread is not None and self._realtime_stop_thread.is_alive())
        ):
            logger.warning("VoiceService start deferred: previous teardown is still running")
            threading.Thread(
                target=self._resume_after_teardown,
                args=(self._lifecycle_revision, self._thread, self._realtime_stop_thread),
                daemon=True, name="voice-restart-wait",
            ).start()
            return
        if self._running:
            return
        if not self.available:
            logger.warning(
                "VoiceService not starting — sd=%s np=%s stt=%s",
                self._sd is not None,
                self._np is not None,
                self._stt.available,
            )
            return
        if self._turn_detector is not None:
            self._turn_detector.close()
            self._turn_detector = None
        self._running = True
        if voice_cfg.TURN_END_ENABLED and not voice_cfg.LIVE_MODE:
            self._turn_detector = SmartTurnDetector()
        if hal_config.REALTIME_ENABLED:
            self._realtime.start()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="voice")
        self._thread.start()
        logger.info("VoiceService started (local VAD + %s)", self._stt.name)

    def _resume_after_teardown(self, revision, voice_thread, realtime_thread):
        for worker in (voice_thread, realtime_thread):
            if worker is not None:
                worker.join()
        with self._lifecycle_lock:
            from hal import app_state

            if (revision == self._lifecycle_revision
                    and not app_state.privacy.mic_locked() and not app_state._mic_muted
                    and not app_state._sleeping and not app_state._enrolling):
                self._start_locked()

    @property
    def harness_capture_active(self) -> bool:
        return self._harness_capture.active

    @property
    def realtime(self) -> RealtimeOrchestrator:
        """The realtime orchestrator, for device-initiated announcements."""
        return self._realtime

    def start_harness_capture(self, snapshot: dict, *, reservation=None) -> bool:
        from hal import app_state

        if app_state._hw_mic_switch_muted is True or app_state._mic_muted or app_state._sleeping:
            return False
        if not self._running or self._tts_is_speaking() or self._music_is_playing():
            return False
        return self._harness_capture.start(snapshot, reservation=reservation)

    def finish_harness_capture(self) -> bool:
        return self._harness_capture.finish()

    def cancel_harness_capture(self) -> None:
        self.device_input.cancel()

    def close(self):
        """Permanently dispose this pipeline; mute/unmute uses stop/start."""
        self.stop()
        worker = self._stt_drain_worker
        if worker is not None:
            worker.shutdown(wait=False, cancel_futures=True)
        queue = getattr(self, "_device_turn_queue", None)
        if queue is not None and not queue.shutdown(timeout=1):
            if self._device_dispose_thread is None:
                def dispose_after_finalizer():
                    queue.shutdown(timeout=None)
                    self._decorator.close()

                self._device_dispose_thread = threading.Thread(
                    target=dispose_after_finalizer, daemon=True, name="device-voice-dispose",
                )
                self._device_dispose_thread.start()
        else:
            self._decorator.close()

    def stop(self, *, background=False):
        self.cancel_automatic_reply()
        self._lifecycle_lock.acquire()
        self._lifecycle_revision += 1
        self._input_mode_resume_pending = False
        self._running = False
        if background:
            try:
                threading.Thread(target=self._stop_and_unlock, daemon=True,
                                 name="voice-mute-teardown").start()
            except BaseException:
                self._lifecycle_lock.release()
                raise
        else:
            self._stop_and_unlock()

    def _stop_and_unlock(self):
        try:
            self._stop_locked()
        finally:
            self._lifecycle_lock.release()

    def _stop_locked(self, *, summarize=True):
        with self._mic_lock:
            if self._active_mic is not None:
                try:
                    self._active_mic.abort()
                except Exception:
                    logger.exception("Failed to abort voice capture")
        self._wakeword_focus.clear()
        self.cancel_harness_capture()
        self._running = False
        if hal_config.REALTIME_ENABLED:
            # realtime.stop() calls _context.summarize_device_memory() +
            # summarize_realtime_memory() which fire LLM requests.
            rt_thread = self._realtime_stop_thread
            if rt_thread is None or not rt_thread.is_alive():
                def stop_realtime():
                    if summarize:
                        self._realtime.stop()
                    else:
                        self._realtime.stop(summarize=False)

                rt_thread = threading.Thread(
                    target=stop_realtime,
                    daemon=True,
                    name="voice-realtime-teardown",
                )
                self._realtime_stop_thread = rt_thread
                rt_thread.start()
            rt_thread.join(timeout=3.0)
            if rt_thread.is_alive():
                logger.warning(
                    "realtime.stop() did not finish in 3s -- restart will wait for teardown"
                )
        if self._thread:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                logger.warning("VoiceService teardown pending: voice thread is still running")
                return
            self._thread = None
        if self._turn_detector is not None:
            self._turn_detector.close()
            self._turn_detector = None
        logger.info("VoiceService stopped")

    @contextmanager
    def _capture(self, backend, rate=None):
        with self._mic_lock:
            if not self._running:
                close = getattr(backend, "close", None)
                if close is not None:
                    close()
                raise InterruptedError("Voice capture stopped")
            context = aec.wrap_mic(backend, rate, self._np) if rate is not None else backend
            mic = context.__enter__()
            self._active_mic = backend
        try:
            yield mic
        finally:
            with self._mic_lock:
                try:
                    context.__exit__(None, None, None)
                finally:
                    self._active_mic = None

    def _get_alsa_device_str(self) -> Optional[str]:
        """Derive ALSA plughw device string from the sounddevice input device index."""
        if self._input_device is None or self._sd is None:
            return None
        try:
            name = self._sd.query_devices(self._input_device)["name"]
            import re as _re

            m = _re.search(r"\(hw:(\d+),(\d+)\)", name)
            if m:
                alsa = f"plughw:{m.group(1)},{m.group(2)}"
                logger.info("ALSA device: %s (from sd device name '%s')", alsa, name)
                return alsa
        except Exception as e:
            logger.debug("Could not extract hw: from sd device name: %s", e)

        try:
            result = subprocess.run(
                ["arecord", "-l"], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                import re as _re

                for line in result.stdout.splitlines():
                    if line.startswith("card "):
                        m = _re.search(r"card (\d+):", line)
                        if m:
                            alsa = f"plughw:{m.group(1)},0"
                            logger.info("ALSA device: %s (from arecord -l)", alsa)
                            return alsa
        except Exception as e:
            logger.debug("arecord -l failed: %s", e)

        return None

    def _detect_device_rate(self) -> int:
        """Detect the highest-quality sample rate the input device supports."""
        sd = self._sd
        try:
            info = sd.query_devices(self._input_device, "input")
            native = int(info["default_samplerate"])
            # Try to open stream at STT_RATE directly — ALSA plughw does SRC transparently.
            try:
                with sd.InputStream(
                    device=self._input_device,
                    samplerate=voice_cfg.STT_RATE,
                    channels=voice_cfg.CHANNELS,
                    dtype="int16",
                    blocksize=512,
                ):
                    pass
                logger.info(
                    "Audio device opened at %dHz natively (no resample needed)",
                    voice_cfg.STT_RATE,
                )
                return voice_cfg.STT_RATE
            except Exception:
                logger.info(
                    "Audio device native rate: %dHz (will resample to %dHz for STT)",
                    native,
                    voice_cfg.STT_RATE,
                )
                return native
        except Exception as e:
            logger.warning(
                "Could not detect device rate, defaulting to %dHz: %s", voice_cfg.STT_RATE, e
            )
            return voice_cfg.STT_RATE

    def _webrtcvad_is_speech(self, data, device_rate: int) -> bool:
        """Run WebRTC VAD on `data` (normal STT path). True if speech or filter off."""
        if self._webrtc_vad is None:
            return True
        return self._webrtc_vad.is_speech(data, device_rate)

    def _silero_is_speech(self, data, device_rate: int) -> bool:
        """Run Silero VAD on `data`. True if speech or filter off."""
        if self._silero_vad is None:
            return True
        return self._silero_vad.is_speech(data, device_rate)

    def _silero_reset_state(self) -> None:
        if self._silero_vad is not None:
            self._silero_vad.reset_state()

    def _rt_noise_is_speech(self, pcm_int16) -> bool:
        """Realtime noise guard: is `pcm_int16` (STT_RATE PCM16 samples) speech?

        Fails open (returns True = treat as speech, commit) on any error so a model
        glitch never drops a real turn.
        """
        if self._rt_noise_vad is None:
            try:
                self._rt_noise_vad = SileroVADFilter(
                    voice_cfg.SILERO_MODEL_PATH, self._np
                )
            except Exception as e:
                logger.warning("Realtime noise-guard Silero load failed: %s", e)
                return True
        try:
            peak, mean, ratio, span_ratio, span_seconds = self._rt_noise_vad.speech_metrics(
                pcm_int16, voice_cfg.STT_RATE
            )
            self._rt_noise_vad.reset_state()
            # Density alone can accept a single noisy 32 ms frame at ratio 1.0.
            # Count voiced frames rather than the whole span or recording duration.
            is_speech = accepts_speech_metrics(
                (peak, mean, ratio, span_ratio, span_seconds),
                min_ratio=hal_config.REALTIME_NOISE_SPEECH_RATIO,
                min_voiced_ms=hal_config.VOICE_NOISE_MIN_VOICED_MS,
            )
            logger.info(
                "[realtime] noise-guard metrics: peak=%.3f mean=%.3f voiced_ratio=%.3f "
                "span_ratio=%.3f span_seconds=%.2f voiced_ms=%.1f "
                "min_ratio=%.2f min_voiced_ms=%.1f accepted=%s",
                peak, mean, ratio, span_ratio, span_seconds,
                span_ratio * span_seconds * 1000,
                hal_config.REALTIME_NOISE_SPEECH_RATIO,
                hal_config.VOICE_NOISE_MIN_VOICED_MS, is_speech,
            )
            return is_speech
        except Exception as e:
            logger.warning("Realtime noise-guard Silero inference failed: %s", e)
            return True

    def _silence_window_is_speech(self, window, device_rate: int) -> bool:
        """Is this above-RMS window real speech, or just a loud room?

        Fails open (True) on any error: a model glitch must never cut somebody off
        mid-sentence.
        """
        if self._silence_vad is None:
            try:
                self._silence_vad = SileroVADFilter(
                    voice_cfg.SILERO_MODEL_PATH, self._np
                )
            except Exception as e:
                logger.warning("Silence-clock Silero load failed: %s", e)
                return True
        if not self._silence_vad.available:
            return True
        try:
            return self._silence_vad.is_speech(window, device_rate)
        except Exception as e:
            logger.warning("Silence-clock Silero inference failed: %s", e)
            return True

    def _tts_is_speaking(self) -> bool:
        """Check if TTS is currently using the audio device."""
        return self._tts is not None and self._tts.speaking

    def _music_is_playing(self) -> bool:
        """Check if music is currently playing."""
        return self._music is not None and self._music.playing

    def _wait_for_tts(self):
        """Block until TTS finishes speaking, then wait for reverb to decay (adaptive RMS gate)."""
        if not self._tts_is_speaking():
            return

        logger.info("TTS is speaking, pausing mic until done...")
        while self._running and self._tts_is_speaking():
            time.sleep(0.2)

        if not self._running:
            return

        # Adaptive RMS gate: wait for reverb/echo to decay instead of fixed sleep
        logger.info("TTS done, waiting for reverb decay (RMS < %d)...", voice_cfg.ECHO_RMS_FLOOR)
        np = self._np
        device_rate = self._device_rate or voice_cfg.STT_RATE
        window_frames = int(device_rate * voice_cfg.ECHO_GATE_WINDOW_S)
        try:
            if self._alsa_device is not None:
                mic_ctx = ArecordStream(
                    alsa_device=self._alsa_device,
                    rate=device_rate,
                    channels=voice_cfg.CHANNELS,
                    blocksize=window_frames,
                    np=np,
                )
            else:
                mic_ctx = self._sd.InputStream(
                    samplerate=device_rate,
                    channels=voice_cfg.CHANNELS,
                    dtype="int16",
                    blocksize=window_frames,
                    device=self._input_device,
                )
            elapsed = 0.0
            with self._capture(mic_ctx) as tmp_mic:
                while elapsed < voice_cfg.ECHO_GATE_MAX_WAIT_S and self._running:
                    data, overflowed = tmp_mic.read(window_frames)
                    if overflowed:
                        continue
                    measured = float(np.sqrt(np.mean(data.astype(np.float32) ** 2)))
                    elapsed += voice_cfg.ECHO_GATE_WINDOW_S
                    if measured < voice_cfg.ECHO_RMS_FLOOR:
                        logger.info(
                            "Reverb decayed (RMS=%.0f < %d) after %.2fs",
                            measured,
                            voice_cfg.ECHO_RMS_FLOOR,
                            elapsed,
                        )
                        return
            logger.info(
                "Reverb gate timeout after %.1fs, resuming anyway", voice_cfg.ECHO_GATE_MAX_WAIT_S
            )
        except Exception as e:
            if self._running:
                logger.warning("RMS gate failed, falling back to fixed delay: %s", e)
                time.sleep(1.0)

    def _loop(self):
        """Main loop: local VAD → STT on speech → disconnect on silence."""
        # Before arecord starts, so the mic child inherits the fast cores too.
        cpu_affinity.pin_current_thread(cpu_affinity.FAST)

        if (getattr(hal_config, "VOICE_INPUT_MODE", "automatic") != "tap_to_talk"
                or not device_manual_mode(read_voice_mode())):
            time.sleep(0.5)

        # Use arecord only when explicitly configured via HAL_AUDIO_INPUT_ALSA.
        if self._alsa_device is not None:
            device_rate = voice_cfg.STT_RATE
            logger.info(
                "Using arecord backend (%s) at %dHz", self._alsa_device, device_rate
            )
        else:
            if self._device_rate is None:
                self._device_rate = self._detect_device_rate()
            device_rate = self._device_rate
            logger.info(
                "Using sounddevice backend (device=%s) at %dHz",
                self._input_device,
                device_rate,
            )

        frame_size = int(device_rate * voice_cfg.FRAME_DURATION_MS / 1000)
        self._device_rate = device_rate
        if getattr(hal_config, "VOICE_INPUT_MODE", "automatic") == "tap_to_talk":
            # Prepare echo-reference filters while idle, without opening the mic.
            # Otherwise the first explicit tap pays SciPy's cold import cost.
            aec.configure(device_rate)

        ready = getattr(self, "_input_mode_ready", None)
        if ready is not None:
            ready.set()

        while self._running:
            mode = read_voice_mode()
            self.device_input.observe(mode)
            manual_capture = self._harness_capture.claim(mode)
            if manual_capture is not None and manual_capture.reservation is not None:
                self._run_device_capture(manual_capture, frame_size, device_rate)
                continue
            if requires_manual_capture(mode) and manual_capture is None:
                # Manual input owns capture: no ambient VAD/recorder while idle.
                if device_manual_mode(mode):
                    self.device_input.wait_for_capture()
                else:
                    time.sleep(0.1)
                continue
            self._wait_for_tts()
            if self._music_is_playing():
                logger.info("Music playing, pausing mic...")
                while self._running and self._music_is_playing():
                    time.sleep(0.5)
                logger.info("Music stopped, resuming mic")

            # Realtime (Gemini Live) holds the ALSA input directly for its full-duplex
            # uplink, so the turn-based arecord path racing it on the same USB mic every
            # ~3s would only fail with "audio open error.
            if self._live_running:
                logger.info("Live session active — pausing turn-based mic loop")
                while self._running and self._live_running:
                    time.sleep(0.5)
                if self._running:
                    logger.info("Live session ended — resuming turn-based mic loop")

            if manual_capture is not None and (
                not self._running or manual_capture.cancelled.is_set()
                or not same_capture_target(manual_capture.snapshot, read_voice_mode())
            ):
                self._harness_capture.release(manual_capture)
                continue
            try:
                if self._alsa_device is not None:
                    mic_ctx = ArecordStream(
                        alsa_device=self._alsa_device,
                        rate=device_rate,
                        channels=voice_cfg.CHANNELS,
                        blocksize=frame_size,
                        np=self._np,
                    )
                else:
                    mic_ctx = self._sd.InputStream(
                        samplerate=device_rate,
                        channels=voice_cfg.CHANNELS,
                        dtype="int16",
                        blocksize=frame_size,
                        device=self._input_device,
                    )
                with self._capture(mic_ctx, device_rate) as mic:
                    if getattr(self, "_live_gate", None) is not None:
                        # Hardware AEC convergence belongs to the open device,
                        # not each logical provider listening window.
                        self._live_gate.reset()
                        self._aec_live_played_start = live_playback.played_seconds()
                    logger.info(
                        "Listening for speech (RMS=%d, rate=%dHz, backend=%s)...",
                        voice_cfg.RMS_THRESHOLD,
                        device_rate,
                        f"arecord({self._alsa_device})"
                        if self._alsa_device
                        else f"sd({self._input_device})",
                    )
                    if manual_capture is not None:
                        try:
                            self._stream_session(
                                mic, frame_size, device_rate,
                                harness_voice=manual_capture.snapshot,
                                manual_capture=manual_capture,
                            )
                        finally:
                            self._harness_capture.release(manual_capture)
                    else:
                        self._vad_loop(mic, frame_size, device_rate)
            except Exception as e:
                if manual_capture is not None:
                    self._harness_capture.release(manual_capture)
                if self._running:
                    logger.warning("Voice loop error: %s", e)
                    time.sleep(3)

    def _run_device_capture(self, capture, frame_size, device_rate):
        """Keep admission guard until mic close; finalization owns only PCM."""
        handed_off = False
        try:
            if (not self.device_input.claim(capture)
                    or not self._device_capture_valid(capture.snapshot)
                    or self._music_is_playing()):
                return
            if self._live_running:
                capture.cancelled.set()
                return
            if self._alsa_device is not None:
                backend = ArecordStream(
                    alsa_device=self._alsa_device, rate=device_rate,
                    channels=voice_cfg.CHANNELS, blocksize=frame_size,
                    np=self._np, low_latency=True,
                )
            else:
                backend = self._sd.InputStream(
                    samplerate=device_rate, channels=voice_cfg.CHANNELS,
                    dtype="int16", blocksize=frame_size, device=self._input_device,
                )
            with self._capture(backend, device_rate) as mic:
                self._device_pipeline.run_capture(
                    mic, frame_size, device_rate, capture, capture.reservation,
                    tts=self.device_input.input_lease(capture),
                )
                handed_off = True
        except Exception:
            capture.cancelled.set()
            logger.exception("Device capture failed")
        finally:
            if not handed_off:
                self._device_turn_queue.release(capture.reservation)
            self._harness_capture.release(capture)
            self.device_input.release(capture)

    def _vad_loop(self, mic, frame_size: int, device_rate: int):
        """Monitor mic with local VAD, connect STT when speech detected.

        Warm-mic mode (HAL_WARM_MIC): never returns for TTS/music.
        """
        speech_start = None
        speech_pre_buffer = []
        lookback = deque(maxlen=voice_cfg.PRE_ROLL_FRAMES)
        draining = False
        bc_muting = False

        keepalive_session = None
        last_keepalive_ping = time.time()

        if stt_keepalive_on := voice_cfg.STT_KEEPALIVE:
            keepalive_session = self._stt.create_session()
            if not keepalive_session.start(lambda text, is_final: None):
                keepalive_session = None
            else:
                logger.info("STT keepalive: pre-connected, waiting for speech...")

        mode_checked_at = 0.0
        while self._running:
            if time.monotonic() - mode_checked_at >= 0.25:
                mode_checked_at = time.monotonic()
                mode = read_voice_mode()
                self.device_input.observe(mode)
                if requires_manual_capture(mode):
                    if keepalive_session:
                        keepalive_session.close()
                    return
            tts_or_music = self._tts_is_speaking() or self._music_is_playing()

            if tts_or_music:
                if not voice_cfg.WARM_MIC:
                    logger.info("TTS/music started, releasing mic...")
                    if keepalive_session:
                        keepalive_session.close()
                    return
                # Warm: keep arecord OPEN, drain + discard so the speaker's audio
                # never reaches STT and the next turn pays no reopen latency.
                if not draining:
                    logger.info("TTS/music active — draining mic (warm, arecord kept open)")
                    if keepalive_session:
                        keepalive_session.close()
                        keepalive_session = None
                    speech_start = None
                    speech_pre_buffer = []
                    draining = True
                mic.read(frame_size)
                continue

            if draining:
                # Skip a short echo window so post-playback reverb doesn't
                # false-trigger, then resume.
                logger.info("TTS/music ended — echo-skip then resume VAD (warm mic)")
                skip_elapsed = 0.0
                while skip_elapsed < voice_cfg.WARM_MIC_ECHO_SKIP_MAX_S and self._running:
                    d, ov = mic.read(frame_size)
                    skip_elapsed += voice_cfg.FRAME_DURATION_MS / 1000.0
                    if not ov and rms(d, self._np) < voice_cfg.ECHO_RMS_FLOOR:
                        break
                # Cleared, and it has to stay that way until something else can tell the
                # device's voice from a person's IN THE TRANSCRIPT on every route.
                lookback.clear()
                self._silero_reset_state()
                draining = False
                if stt_keepalive_on and self._running and not self._tts_is_speaking():
                    keepalive_session = self._stt.create_session()
                    if not keepalive_session.start(lambda text, is_final: None):
                        keepalive_session = None
                    else:
                        logger.info("STT keepalive: pre-connected, waiting for speech...")
                continue

            data, overflowed = mic.read(frame_size)
            if overflowed:
                continue

            # Re-check after blocking read — music/TTS may have started during mic.read
            if self._tts_is_speaking() or self._music_is_playing():
                if not voice_cfg.WARM_MIC:
                    return
                continue

            # Our own backchannel cue is in the room: it bypasses the TTS `speaking`
            # flag on purpose (that flag would kill the running STT session), so nothing
            # above filters it.
            if self._backchannel.self_audio_active:
                if not bc_muting:
                    bc_muting = True
                    logger.info("Backchannel cue in the room — VAD muted until it decays")
                if speech_start is not None:
                    speech_start = None
                    speech_pre_buffer = []
                continue
            if bc_muting:
                # Same cleanup the warm-mic drain does on resume: the dropped
                # frames are a discontinuity for Silero's LSTM, and the lookback
                # must not pre-roll audio from before the cue.
                bc_muting = False
                lookback.clear()
                self._silero_reset_state()
                logger.info("Backchannel cue decayed — VAD resumed")

            lookback.append(data)

            if (
                keepalive_session is not None
                and speech_start is None
                and (time.time() - last_keepalive_ping) >= voice_cfg.STT_KEEPALIVE_PING_S
                and hasattr(keepalive_session, "send_keepalive")
            ):
                keepalive_session.send_keepalive()
                last_keepalive_ping = time.time()

            energy = rms(data, self._np)
            self._mic_level = energy
            self._mic_level_ts = time.time()

            aec_live_entry = self._hardware_aec_live_entry()
            if self._vad_entry_is_speech(data, device_rate, energy):
                if speech_start is None:
                    speech_start = time.time()
                    speech_pre_buffer = [data]
                else:
                    speech_pre_buffer.append(data)
                held_s = (sum(len(frame) for frame in speech_pre_buffer) / device_rate
                          if aec_live_entry else time.time() - speech_start)
                if held_s >= (0.16 if aec_live_entry else voice_cfg.SPEECH_HOLDOFF_S):
                    if not aec_live_entry and self._silero_vad is not None:
                        combined = self._np.concatenate(speech_pre_buffer)
                        if not self._silero_is_speech(combined, device_rate):
                            speech_start = None
                            speech_pre_buffer = []
                            continue
                    buffered = len(speech_pre_buffer)
                    history = (
                        list(lookback)[:-buffered] if buffered > 0 else list(lookback)
                    )
                    all_frames = history + speech_pre_buffer
                    logger.info(
                        "Speech detected (RMS=%.0f) — pre-roll=%d frames (~%dms) "
                        "+ holdoff=%d frames | pre-roll RMS: %s",
                        energy,
                        len(history),
                        len(history) * voice_cfg.FRAME_DURATION_MS,
                        buffered,
                        " ".join("%.0f" % rms(f, self._np) for f in history),
                    )
                    # Classify the turn before gaze can open/refresh the window.
                    # A gaze opener must keep its cues; a refresh is still a follow-up.
                    wake_focus_at_entry = (
                        hal_config.WAKEWORD_ENABLED and self._wakeword_focus.is_active()
                    )
                    gaze_focus_granted = False
                    try:
                        from hal.drivers.tracking import gaze

                        gaze_focus_granted = gaze.on_speech_start()
                    except Exception as e:
                        logger.debug("gaze wake check skipped: %s", e)
                    pending_listening_cue_id = None
                    if gaze_focus_granted:
                        # Gaze + VAD has already proved intent, while STT normally needs
                        # another 1.5-2.5s for its first partial.
                        from hal import app_state

                        pending_listening_cue_id = app_state.show_listening_pending_cue()
                    speech_pre_buffer = [
                        resample_to_stt(f, device_rate, voice_cfg.STT_RATE, self._np)
                        for f in all_frames
                    ]
                    # THE handover. In live mode the VAD's whole job ends here: it has
                    # decided somebody is talking to the device, and the session it
                    # opens does its own endpointing from now on.
                    harness_voice = read_voice_mode()
                    decision = (
                        self._live_decision(speech_pre_buffer)
                        if voice_cfg.LIVE_MODE and not bypass_realtime(harness_voice)
                        else "turn"
                    )
                    if decision == "live":
                        if self._live_session(
                            mic, frame_size, device_rate, speech_pre_buffer,
                            harness_voice=harness_voice,
                        ):
                            logger.info(
                                "[live] reopening the mic after playback — see "
                                "the capture-corruption note in _live_session"
                            )
                            return
                    elif decision == "turn":
                        if self._stream_session(
                            mic,
                            frame_size,
                            device_rate,
                            preconnected_session=keepalive_session,
                            speech_pre_buffer=speech_pre_buffer,
                            pending_listening_cue_id=pending_listening_cue_id,
                            harness_voice=harness_voice,
                            wake_focus_at_entry=wake_focus_at_entry,
                        ):
                            return
                    keepalive_session = None
                    speech_start = None
                    speech_pre_buffer = []
                    if decision != "skip":
                        lookback.clear()
                    self._silero_reset_state()
                    logger.info("VAD resumed — mic active, waiting for next speech")
                    with self._automatic_reply_lock:
                        cancelled_at = self._automatic_reply_cancelled_at
                        self._automatic_reply_cancelled_at = None
                    if cancelled_at is None:
                        time.sleep(voice_cfg.SESSION_COOLDOWN_S)
                    else:
                        logger.info("[automic-stop] receive released; VAD resumed after %.1fms",
                                    (time.monotonic() - cancelled_at) * 1000)
                    if stt_keepalive_on and self._running and not self._tts_is_speaking():
                        keepalive_session = self._stt.create_session()
                        if not keepalive_session.start(lambda text, is_final: None):
                            keepalive_session = None
                        else:
                            logger.info(
                                "STT keepalive: pre-connected, waiting for speech..."
                            )
            else:
                speech_start = None
                speech_pre_buffer = []
                if energy >= voice_cfg.RMS_THRESHOLD:
                    logger.debug(
                        "VAD: RMS=%.0f above threshold but Silero rejected — not speech",
                        energy,
                    )

    def _hardware_aec_live_entry(self) -> bool:
        return (isinstance(getattr(self, "_live_gate", None), AdaptiveLiveGate)
                and hal_config.REALTIME_ENABLED
                and (not hal_config.WAKEWORD_ENABLED or self._wakeword_focus.is_active()))

    def _vad_entry_is_speech(self, data, rate, energy):
        if self._hardware_aec_live_entry():
            threshold = self._live_gate.idle_threshold(energy / 32768.0, len(data) / rate)
            return energy >= threshold * 32768.0
        return (energy >= voice_cfg.RMS_THRESHOLD
                and self._webrtcvad_is_speech(data, rate))

    def _live_decision(self, pre_roll: list) -> str:
        """What this VAD trigger should become: "live", "turn" or "skip".

        "skip" costs nothing at all, which is the point: a click must not open a billed
        live session OR an STT session.
        """
        if not hal_config.REALTIME_ENABLED:
            return "turn"

        if self._music_is_playing():
            logger.info("[live] music playing — not opening a live session")
            return "skip"

        if pre_roll and not VoiceService._hardware_aec_live_entry(self):
            try:
                pcm = self._np.frombuffer(b"".join(pre_roll), dtype=self._np.int16)
                if not self._rt_noise_is_speech(pcm):
                    logger.info(
                        "[live] trigger was loud but NOT speech — no session opened"
                    )
                    return "skip"
            except Exception as e:
                logger.warning("[live] speech gate failed, allowing: %s", e)
        if (
            hal_config.WAKEWORD_ENABLED
            and not self._wakeword_focus.is_active()
        ):
            logger.info("[live] wake focus closed — using STT to check the wake phrase")
            return "turn"

        self._realtime.prepare_turn()

        if not self._realtime.wait_until_available(5.0):
            logger.info("[live] realtime unavailable — using the turn path")
            return "turn"
        return "live"

    def _live_uplink_frame(self, data):
        """What this mic frame contributes to the uplink.

        A frame the canceller cannot vouch for is REPLACED BY SILENCE OF THE SAME
        LENGTH, never dropped: the uplink is a clock, and a splice is exactly what a
        server-side VAD reads as an onset.
        """
        # Two independent "is the speaker live" signals, because neither alone is
        # enough.
        now = time.monotonic()
        speaking = self._tts_is_speaking()
        if self._live_playback_was_speaking and not speaking:
            self._live_playback_tail_until = now + voice_cfg.LIVE_PLAYBACK_TAIL_S
        self._live_playback_was_speaking = speaking
        if (
            not speaking
            and now >= self._live_playback_tail_until
            and aec.reference_idle_for() > voice_cfg.LIVE_PLAYBACK_TAIL_S
        ):
            return data
        self._live_frames_during_playback += 1
        # "always": hand the provider every frame and let ITS VAD do the separating.
        if voice_cfg.LIVE_UPLINK_DURING_PLAYBACK == "always":
            return data
        if (
            voice_cfg.LIVE_UPLINK_DURING_PLAYBACK == "cancelled"
            and not aec.uncancelled()
        ):
            return data
        self._live_frames_substituted += 1
        return self._np.zeros_like(data)

    def _live_emotion_addressed(self, text: str, harness_voice=None) -> bool:
        """Mirror the regular turn's addressing gate for LIVE reactions."""
        harness_listening = bool(
            harness_voice and harness_voice.get("enabled")
            and not harness_voice.get("unavailable", False)
        )
        return harness_listening or is_addressed(
            hal_config.WAKEWORD_ENABLED,
            bool(text) and self._decorator.starts_with_wake_word(text),
            False,
            self._wakeword_focus.is_active(),
        )

    def _live_stop_output(self) -> None:
        """A LIVE output reset/session exit must not stop a main-agent reply."""
        if self._tts is None:
            return
        if self._tts.realtime_speaking:
            self._tts.stop(preserve_main_queue=True)

    def _live_quiet_for(self, now, user_spoke_at, last_reply_end):
        return now - max(user_spoke_at, last_reply_end,
                         self._live_last_model_output)

    def _live_idle_pending_speech(self, now, quiet_for, last_user_speech):
        """Give a pending hardware-AEC utterance one bounded transcript window."""
        timeout = voice_cfg.LIVE_IDLE_HANGUP_S
        if quiet_for <= timeout:
            self._live_idle_speech_deadline = 0.0
            return False
        if not voice_cfg.LIVE_IDLE_REQUIRES_TRANSCRIPT or getattr(self, "_live_gate", None) is None:
            return False
        deadline = self._live_idle_speech_deadline
        if not deadline:
            recent_s = max(1.0, hal_config.LIVE_VAD_SILENCE_MS / 1000) + 1.0
            if now - last_user_speech > recent_s:
                return False
            self._live_idle_speech_deadline = deadline = now + timeout
            logger.info("[live] idle hangup deferred for pending speech (max %.1fs)", timeout)
        return now < deadline

    def _aec_live_interrupt_reply(self, reason, key=None):
        if reason == "local_speech":
            # Energy can be residual speaker echo. Match the demo's duck mode:
            # reduce volume reversibly, but only the provider may cancel a reply.
            live_playback.duck(True)
            logger.info("[live-aec] local speech candidate: duck only; awaiting provider interrupt")
            return False
        guard = getattr(self, "_aec_live_replies", None)
        if guard is None:
            return
        before = self._tts_is_speaking()
        affected = guard.cancel(self._tts.stop_realtime_reply, key=key)
        if affected:
            gate = getattr(self, "_live_gate", None)
            if gate is not None:
                gate.clear_duck()
            live_playback.duck(False)
        logger.info("[live-aec] interrupt requested reason=%s reply=%s affected=%s speaking_before=%s",
                    reason, key, affected, before)
        return affected

    def _live_out_pump(self, generation: int, harness_voice=None, cues=None, opener=None, stop_event=None) -> None:
        """Play what the model says, for as long as the session lasts."""
        input_text = {}
        input_focus = {}
        addressed_inputs = set()
        response_inputs = set()
        focus_refreshed = set()
        focus_held = set()
        focus_finished = set()
        rejected_inputs = set()
        harness_listening = bool(
            harness_voice and harness_voice.get("enabled")
            and not harness_voice.get("unavailable", False)
        )
        focus = getattr(self, "_wakeword_focus", None)
        focus_at_start = bool(focus and focus.is_active())

        def classify_input(key, text):
            if not text.strip():
                return ""
            if key not in input_focus:
                input_focus[key] = bool(focus_at_start or (focus and focus.is_active()))
            try:
                _, kind = self._decorator.classify_wake_word(text)
            except Exception:
                logger.debug("[live] wake classification unavailable", exc_info=True)
                return ""  # Diagnostics must never block speech or delegation.
            if (kind == "voice" and hal_config.WAKEWORD_ENABLED
                    and input_focus[key] and not harness_listening):
                kind = "voice_followup"
            return kind

        def hold_live_focus(key, text=""):
            text = text or input_text.get(key, "")
            if (not key or key in focus_held or key in rejected_inputs
                    or not text.strip() or focus is None
                    or not hal_config.WAKEWORD_ENABLED or harness_listening):
                return
            if not (focus_at_start or key in addressed_inputs
                    or self._live_emotion_addressed(text, harness_voice)):
                return
            iid = metrics.interaction(key)
            if iid and focus.begin(iid):
                focus_held.add(key)

        def refresh_focus(key, text=""):
            hold_live_focus(key, text)
            if key in focus_held:
                focus_refreshed.add(key)

        live_replies = getattr(self, "_aec_live_replies", None)

        def speak_live(text, iid, key, first=False):
            def enqueue():
                if not first or not self._tts.speak(text, turn_id=iid, realtime_reply=True):
                    self._tts.speak_queue(text, turn_id=iid, realtime_reply=True)
            if live_replies is None:
                enqueue()
                return True
            return live_replies.play(key, enqueue)
        metrics = LiveVoiceMetrics()
        history = LiveHistory(
            self._sensing_sender, harness_voice,
            self.strip_rt_markers,
        )

        def bind_opener(key):
            if opener is None or not key or opener["key"]:
                return
            opener["key"] = key
            metrics.seed(key, opener["interaction_id"])
            input_text[key] = opener["transcript"]
            addressed_inputs.add(key)
            history.input(key, opener["transcript"], opener["interaction_id"],
                          opener["voice_turn_type"])
            hold_live_focus(key)

        def complete_metrics(key, completed):
            if key in rejected_inputs:
                return
            if opener is not None and key and key == opener["key"]:
                if completed:
                    opener["execution_completed"] = True
                if not opener.get("replied", False):
                    return
            metrics.complete(key, completed)

        deferred_marker_tail = None
        fallback_sequence = 0
        while self._live_running and generation == self._live_generation:
            fallback_sequence += 1
            fallback_key = ("unkeyed", generation, fallback_sequence)
            native_started = False
            transcript = ""
            native = hal_config.REALTIME_NATIVE_AUDIO or native_voice(self._tts) is not None
            sentence_buf = ""
            buffer_reply_key = ""
            first_sent = False
            speech_iid = ""
            buffer_mixed = False
            native_owner = ""
            native_pending = []
            native_pending_samples = 0
            native_pending_key = None
            native_failed_keys = set()
            try:
                output_options = {"stop_event": stop_event} if stop_event is not None else {}
                for out in self._realtime.stream_output(**output_options):
                    if not (self._live_running and generation == self._live_generation):
                        break
                    if (isinstance(out, (RTAudioOutput, RTTextOutput, RTTextSegmentEndOutput, ExecutionOutput))
                            and out.user_turn_id and out.user_turn_id in rejected_inputs):
                        continue
                    if (deferred_marker_tail is not None
                            and isinstance(out, (RejectSignal, DelegateSignal, RTInterruptedOutput))
                            and (not out.user_turn_id or out.user_turn_id == deferred_marker_tail[0])):
                        deferred_marker_tail = None
                    if live_replies is not None:
                        if sentence_buf and not live_replies.allowed(buffer_reply_key):
                            sentence_buf = ""
                            first_sent = False
                        if isinstance(out, (RTAudioOutput, RTTextOutput)):
                            key = getattr(out, "user_turn_id", "") or fallback_key
                            if not live_replies.observe(key):
                                continue
                    if isinstance(out, (UserSpeechOutput, RTAudioOutput, RTTextOutput, DelegateSignal, RejectSignal)):
                        bind_opener(getattr(out, "user_turn_id", "") or getattr(out, "turn_id", ""))
                    if isinstance(out, UserSpeechOutput):
                        if opener is not None and out.turn_id and out.turn_id != opener["key"]:
                            # Never replay an older request into main after
                            # the user has moved on to another live input.
                            opener["consumed"] = True
                        if out.turn_id and out.transcript.strip():
                            self._live_last_transcript_at = time.time()
                            text = merge_stt_hypothesis(
                                input_text.get(out.turn_id, ""), out.transcript,
                            )
                            input_text[out.turn_id] = text
                            if (out.turn_id not in addressed_inputs
                                    and self._live_emotion_addressed(text, harness_voice)):
                                addressed_inputs.add(out.turn_id)
                                same_reply = (
                                    out.turn_id in response_inputs
                                    and self._tts is not None
                                    and self._tts.realtime_speaking
                                )
                                if self._tts is not None and self._tts.speaking and not same_reply:
                                    self._tts.stop()
                        if cues is not None:
                            cues.input(
                                out.turn_id, out.endpoint_at, transcript=out.transcript,
                                transcript_finished=out.transcript_finished,
                            )
                        if out.transcript_finished and not out.transcript and out.endpoint_at is None:
                            continue
                        iid = metrics.speech(out.turn_id, out.endpoint_at, out.method)
                        # Classify before this input opens/holds its own focus window.
                        turn_type = classify_input(out.turn_id, input_text.get(out.turn_id, out.transcript))
                        hold_live_focus(out.turn_id)
                        history.input(
                            out.turn_id,
                            "" if opener is not None and out.turn_id == opener["key"] else out.transcript,
                            iid,
                            turn_type,
                        )
                        continue
                    if isinstance(out, ExecutionOutput):
                        if out.execution_completed:
                            refresh_focus(out.user_turn_id)
                        elif focus is not None:
                            focus.finish(metrics.interaction(out.user_turn_id), cancelled=True)
                        if cues is not None:
                            cues.finish(out.user_turn_id)
                        if opener is None or opener["consumed"] or out.user_turn_id != opener["key"]:
                            history.complete(out.user_turn_id, out.execution_completed)
                        complete_metrics(out.user_turn_id, out.execution_completed)
                        continue
                    if isinstance(out, RejectSignal):
                        if opener is not None:
                            opener["consumed"] = True
                        rejected_inputs.add(out.user_turn_id)
                        iid = metrics.interaction(out.user_turn_id)
                        if iid and self._tts is not None:
                            self._tts.stop_realtime_reply(turn_id=iid)
                        if out.user_turn_id and buffer_reply_key == out.user_turn_id:
                            sentence_buf = ""
                            first_sent = False
                        if iid and native_started and native_owner == "interaction:" + iid:
                            self._tts.native_play_end("")
                            native_started = False
                        logger.info("[live] rejected output cancelled turn=%s interaction=%s",
                                    out.user_turn_id, iid)
                        if focus is not None:
                            focus.finish(metrics.interaction(out.user_turn_id), cancelled=True)
                        if cues is not None:
                            cues.finish(out.user_turn_id)
                        history.discard(out.user_turn_id)
                        metrics.reject(out.user_turn_id)
                        continue
                    if isinstance(out, DelegateSignal):
                        if opener is not None:
                            opener["consumed"] = True
                        if cues is not None:
                            cues.close()
                        history.discard(out.user_turn_id)
                        # Hang up so the main agent's reply does not play
                        # into an open uplink.
                        logger.info("[live] model delegated → forwarding to OS server")
                        self._live_running = False
                        if out.transcript:
                            self._realtime.save_main_handoff(out.transcript)
                        key = out.user_turn_id or "delegate"
                        if opener is not None and (not opener["key"] or key == opener["key"]):
                            opener["key"] = key
                            metrics.seed(key, opener["interaction_id"])
                        iid = metrics.interaction(key) or metrics.speech(
                            key, None, "provider_delegate",
                        )
                        refresh_focus(key, out.transcript)
                        dispatch_turn(
                            self._decorator,
                            self._sensing_sender,
                            out.transcript or (opener["transcript"] if opener is not None and key == opener["key"] else ""),
                            [],
                            [],
                            RealtimeTurnResult(
                                delegated=True,
                                delegate_msg=out.message,
                                handoff_context=out.handoff_context,
                                route=ROUTE_DELEGATED,
                            ),
                            harness_voice=harness_voice,
                            interaction_id=iid,
                            voice_turn_type=classify_input(key, out.transcript or input_text.get(key, "")),
                        )
                        refresh_focus(key, out.transcript)
                        break
                    if isinstance(out, EndCallSignal):
                        if opener is not None:
                            opener["consumed"] = True
                        # Do NOT tear down here.
                        self._live_hangup_at = (
                            time.time() + voice_cfg.LIVE_HANGUP_GRACE_S
                        )
                        logger.info(
                            "[live] model ended the conversation — hanging up in %.0fs",
                            voice_cfg.LIVE_HANGUP_GRACE_S,
                        )
                        continue
                    if isinstance(out, RTInterruptedOutput):
                        if not out.user_turn_id or out.user_turn_id == native_pending_key:
                            native_pending.clear()
                            native_pending_samples = 0
                        if out.reason == "server_interrupt":
                            if getattr(self, "_live_gate", None) is not None:
                                logger.info(
                                    "[live-aec] interrupt received: tts_speaking=%s realtime=%s native=%s buffered_chars=%d",
                                    self._tts_is_speaking(), bool(self._tts and self._tts.realtime_speaking),
                                    native_started, len(sentence_buf),
                                )
                                if self._aec_live_interrupt_reply("provider", out.user_turn_id or None):
                                    sentence_buf = ""
                                    first_sent = False
                                    if native_started:
                                        self._tts.native_play_end(transcript)
                                        native_started = False
                                    transcript = ""
                                    logger.info("[live-aec] provider interrupt: stop requested; confirm playback state in live-aec")
                            if opener is not None:
                                opener["consumed"] = True
                            rejected_inputs.add(out.user_turn_id)
                            if cues is not None:
                                cues.finish(out.user_turn_id)
                            history.discard(out.user_turn_id)
                            iid = metrics.interaction(out.user_turn_id)
                            if focus is not None:
                                focus.finish(iid, cancelled=True)
                            has_pending = getattr(self._tts, "has_pending_speech", lambda owner: False)
                            pending_audio = bool(iid) and (
                                has_pending("run:" + iid) or has_pending("interaction:" + iid)
                            )
                            metrics.interrupt(out.user_turn_id, out.at, pending_audio=pending_audio)
                            # Observe only. Playback keeps its existing stop/reset
                            # behavior; instrumentation must not add a new stop.
                            continue
                        history.reset_output(out.user_turn_id)
                        self._live_stop_output()
                        if native_started:
                            self._tts.native_play_end(transcript)
                            native_started = False
                            transcript = ""
                        continue
                    if isinstance(out, RTAudioOutput):
                        self._live_last_model_output = time.time()
                        if not native:
                            continue
                        if out.user_turn_id:
                            response_inputs.add(out.user_turn_id)
                        if cues is not None:
                            cues.finish(out.user_turn_id)
                        def write_native():
                            nonlocal native_started, native_owner
                            nonlocal native_pending_key, native_pending_samples
                            key = out.user_turn_id or fallback_key
                            if key in native_failed_keys:
                                return
                            if key != native_pending_key:
                                native_pending.clear()
                                native_pending_samples = 0
                                native_pending_key = key
                            owner = metrics.owner(out.user_turn_id)
                            if native_started and owner != native_owner:
                                self._tts.set_native_playback_owner(owner)
                                native_owner = owner
                            if not native_started:
                                native_pending.append(out.audio)
                                native_pending_samples += len(out.audio)
                                if native_pending_samples > self._realtime.output_sample_rate * 30:
                                    logger.warning("[live] Native speaker unavailable for 30s of audio; cancelling reply=%s", key)
                                    native_pending.clear()
                                    native_pending_samples = 0
                                    native_failed_keys.add(key)
                                    return
                                native_started = self._tts is not None and (
                                    self._tts.native_play_begin(
                                        self._realtime.output_sample_rate, owner=owner,
                                    )
                                )
                                native_owner = owner
                            if native_started:
                                if opener is not None:
                                    opener["consumed"] = True
                                frames = native_pending or [out.audio]
                                for frame in frames:
                                    if self._tts.native_play_frame(frame) is False:
                                        # Do not resume with a later suffix after
                                        # cancellation or a failed device write.
                                        native_failed_keys.add(key)
                                        logger.warning("[live] Native playback stopped; discarding remaining audio reply=%s", key)
                                        break
                                native_pending.clear()
                                native_pending_samples = 0
                                if opener is not None:
                                    if out.user_turn_id and out.user_turn_id == opener["key"]:
                                        opener["replied"] = True
                        if live_replies is None:
                            write_native()
                        else:
                            live_replies.play(out.user_turn_id or fallback_key, write_native)
                        if out.transcript:
                            history.output(out.user_turn_id, out.transcript)
                            transcript += out.transcript
                        continue
                    if isinstance(out, RTTextSegmentEndOutput):
                        # A boundary cannot acquire ownership or complete a turn.
                        # Only flush speech already accepted for this exact reply.
                        if (native or buffer_mixed or not sentence_buf
                                or (out.user_turn_id or fallback_key) != buffer_reply_key
                                or self._pending_rt_silence_marker(sentence_buf)):
                            continue
                        visible = realtime_visible_text(sentence_buf, self._tts, self.strip_rt_markers)
                        speech = realtime_speech_text(sentence_buf, self._tts, self.strip_rt_markers) if visible else ""
                        if speech:
                            logger.info("[live] Completed text segment → speak: %r", speech[:80])
                            if cues is not None:
                                cues.finish(out.user_turn_id)
                            speak_live(speech, speech_iid, buffer_reply_key, first=not first_sent)
                            first_sent = True
                            sentence_buf = ""
                            if opener is not None:
                                opener["consumed"] = True
                                if speech_iid == opener["interaction_id"]:
                                    opener["replied"] = True
                        continue
                    if isinstance(out, RTTextOutput):
                        if (not native and getattr(self._tts, "_provider", None) == "elevenlabs"
                                and sentence_buf and out.user_turn_id
                                and buffer_reply_key and out.user_turn_id != buffer_reply_key):
                            # A held sentence belongs only to its original reply.
                            # A newer reply must never flush old speech or tags.
                            sentence_buf = ""
                            first_sent = False
                            buffer_mixed = False
                        if deferred_marker_tail is not None:
                            owner, prefix = deferred_marker_tail
                            if out.user_turn_id == owner:
                                sentence_buf = prefix + sentence_buf
                                buffer_reply_key = owner
                                speech_iid = metrics.interaction(owner)
                            # Never attach a held fragment to a different turn.
                            deferred_marker_tail = None
                        if out.user_turn_id:
                            response_inputs.add(out.user_turn_id)
                        if not transcript and out.text:
                            logger.info("[tts-timing] stage=realtime_first_text mode=live owner=%s reply=%s",
                                        metrics.interaction(out.user_turn_id), out.user_turn_id)
                        history.output(out.user_turn_id, out.text)
                        self._live_last_model_output = time.time()
                        transcript += out.text
                        if native:
                            continue
                        iid = metrics.interaction(out.user_turn_id)
                        if not iid:
                            metrics.owner(out.user_turn_id)
                        if not sentence_buf:
                            buffer_reply_key = out.user_turn_id or fallback_key
                            speech_iid = iid
                            buffer_mixed = False
                        elif iid != speech_iid:
                            # Do not change chunking or flush early for metrics.
                            # A mixed-owner sentence is explicitly unattributed.
                            buffer_mixed = True
                        if buffer_mixed:
                            speech_iid = ""
                        iid = speech_iid
                        sentence_buf += out.text
                        visible = realtime_visible_text(sentence_buf, self._tts, self.strip_rt_markers)
                        if not visible:
                            continue
                        if not first_sent:
                            head, rest = split_realtime_first_chunk(sentence_buf, self._tts, self.strip_rt_markers)
                            head = realtime_speech_text(head, self._tts, self.strip_rt_markers) if head else ""
                            if head:
                                if cues is not None:
                                    cues.finish(out.user_turn_id)
                                if opener is not None:
                                    opener["consumed"] = True
                                speak_live(head, iid, buffer_reply_key, first=True)
                                if opener is not None:
                                    opener["consumed"] = True
                                    if iid == opener["interaction_id"]:
                                        opener["replied"] = True
                                first_sent = True
                                sentence_buf = rest
                        # Check the spoken text; a trailing tag must not hold a
                        # complete sentence until the provider's routing grace ends.
                        sentence = realtime_visible_text(sentence_buf, self._tts, self.strip_rt_markers)
                        complete = sentence.rstrip().endswith(SENTENCE_ENDS + ("…",))
                        ready, tail = ("", "") if complete else split_completed_prefix(sentence_buf)
                        if getattr(self._tts, "_provider", None) == "elevenlabs":
                            ready, tail = split_delivery_sentence(sentence_buf)
                            sentence = realtime_visible_text(ready, self._tts, self.strip_rt_markers)
                        elif ready:
                            sentence = realtime_visible_text(ready, self._tts, self.strip_rt_markers)
                        if sentence.rstrip().endswith(SENTENCE_ENDS + ("…",)):
                            if sentence:
                                if cues is not None:
                                    cues.finish(out.user_turn_id)
                                if opener is not None:
                                    opener["consumed"] = True
                                speech = realtime_speech_text(ready if ready else sentence_buf, self._tts, self.strip_rt_markers)
                                speak_live(speech, iid, buffer_reply_key)
                                if opener is not None:
                                    if iid == opener["interaction_id"]:
                                        opener["replied"] = True
                            sentence_buf = tail if ready else ""
                        continue
                if stop_event is not None and stop_event.is_set():
                    break
                if getattr(self._realtime, "execution_completed", False) is True:
                    refresh_focus(getattr(self._realtime, "execution_turn_id", ""))
                if cues is not None:
                    terminal_key = getattr(self._realtime, "execution_turn_id", "")
                    if terminal_key:
                        cues.finish(terminal_key)
                complete_metrics(
                    getattr(self._realtime, "execution_turn_id", ""),
                    getattr(self._realtime, "execution_completed", False) is True,
                )
            except Exception as e:
                logger.warning("[live] output pump error: %s", e)
                time.sleep(0.2)
            finally:
                if native_started:
                    self._tts.native_play_end(transcript)

                # A stopped promoted session has handed its unanswered opener
                # back to main. A late receive exit must not enqueue its old tail.
                if (not native and sentence_buf.strip()
                        and self._live_running and generation == self._live_generation
                        and not (stop_event is not None and stop_event.is_set())):
                    visible_tail = realtime_visible_text(sentence_buf, self._tts, self.strip_rt_markers)
                    if (self._pending_rt_silence_marker(sentence_buf)
                            and not getattr(self._realtime, "execution_completed", False)):
                        if isinstance(buffer_reply_key, str) and buffer_reply_key:
                            deferred_marker_tail = (buffer_reply_key, sentence_buf)
                        tail = ""
                    else:
                        tail = realtime_speech_text(sentence_buf, self._tts, self.strip_rt_markers) if visible_tail else ""
                    if tail:
                        if cues is not None:
                            cues.finish(getattr(self._realtime, "execution_turn_id", ""))
                        if opener is not None:
                            opener["consumed"] = True
                        speak_live(tail, speech_iid, buffer_reply_key)
                        if opener is not None:
                            if speech_iid == opener["interaction_id"]:
                                opener["replied"] = True
            if opener is not None and opener.get("replied", False):
                complete_metrics(opener["key"], opener.get("execution_completed", False))
            for key in focus_refreshed - focus_finished:
                if opener is not None and key == opener["key"] and not opener["consumed"]:
                    continue
                focus.finish(metrics.interaction(key))
                focus_finished.add(key)
            if (self._live_running and generation == self._live_generation
                    and (opener is None or opener["consumed"])):
                history.complete(
                    getattr(self._realtime, "execution_turn_id", ""),
                    getattr(self._realtime, "execution_completed", False) is True,
                )
            if opener is not None and not opener["consumed"]:
                self._live_running = False
            if transcript:
                self._live_unprompted_replies += 1
                logger.info("[live] model said: %r", transcript[:120])
        for key in focus_held - focus_finished:
            if opener is not None and key == opener["key"] and not opener["consumed"]:
                continue
            focus.finish(metrics.interaction(key), cancelled=True)
        history.close()
        metrics.close()

    def _live_session(
        self, mic, frame_size: int, device_rate: int, pre_roll: list,
        harness_voice=None, opener=None,
    ) -> bool:
        """Stream the mic continuously until the model or the clock ends it.

        Returns True when the caller must REOPEN the mic before listening again — see
        the capture-corruption note in the `finally` below.
        """
        harness_voice = read_voice_mode() if harness_voice is None else harness_voice
        if bypass_realtime(harness_voice):
            return False
        if getattr(self, "_live_gate", None) is not None:
            self._live_gate.reset()
            if not hasattr(self, "_aec_live_played_start"):
                self._aec_live_played_start = live_playback.played_seconds()
            self._aec_live_replies = LiveReplyGuard()
            live_playback.duck(False)
            logger.info("[live-aec] adaptive echo gate active; local duck enabled")
        next_mode_check = time.monotonic() + 0.5
        self._live_generation += 1
        generation = self._live_generation
        self._live_running = True
        self._live_last_model_output = time.time()
        self._live_frames = 0
        self._live_frames_during_playback = 0
        self._live_frames_substituted = 0
        self._live_unprompted_replies = 0
        self._live_last_transcript_at = 0.0
        self._live_hangup_at = 0.0
        started = time.time()
        uplink_dump = None
        if voice_cfg.LIVE_UPLINK_DUMP_DIR:
            try:
                import wave as _wave

                os.makedirs(voice_cfg.LIVE_UPLINK_DUMP_DIR, exist_ok=True)
                _p = os.path.join(
                    voice_cfg.LIVE_UPLINK_DUMP_DIR,
                    "uplink-%s.wav" % time.strftime("%Y%m%d-%H%M%S"),
                )
                uplink_dump = _wave.open(_p, "wb")
                uplink_dump.setnchannels(1)
                uplink_dump.setsampwidth(2)
                uplink_dump.setframerate(voice_cfg.STT_RATE)
                logger.info("[live] uplink dump -> %s", _p)
            except Exception as e:
                logger.warning("[live] uplink dump could not be opened: %s", e)
                uplink_dump = None
        last_user_speech = started
        self._live_idle_speech_deadline = 0.0
        last_reply_end = started
        silence_probe: list = []
        if self._silence_vad is not None:
            self._silence_vad.reset_state()
        # Suppress the post-turn session recycles for the whole session: every
        # one of them would swap the agent out from under this open uplink.
        self._realtime.set_live_active(True)
        logger.info(
            "[live] session START — uplink_during_playback=%s, aec=%s, "
            "idle_hangup=%.0fs, max_unprompted_replies=%d, max=%.0fs, "
            "pre_roll=%d frames",
            voice_cfg.LIVE_UPLINK_DURING_PLAYBACK,
            "on" if aec.active() else "OFF (mic carries full bleed)",
            voice_cfg.LIVE_IDLE_HANGUP_S,
            voice_cfg.LIVE_MAX_UNPROMPTED_REPLIES,
            voice_cfg.LIVE_MAX_S,
            len(pre_roll),
        )
        self._realtime.flush_output()
        cues = LiveVoiceCues(
            addressed=lambda text: self._live_emotion_addressed(text, harness_voice),
        )
        pump_stop = threading.Event()
        pump = threading.Thread(
            target=self._live_out_pump,
            args=(generation, harness_voice, cues, opener, pump_stop),
            daemon=True,
            name="live-out",
        )
        pump.start()
        try:
            for frame in pre_roll:
                if uplink_dump is not None:
                    uplink_dump.writeframes(frame)
                self._realtime.append_audio(self._to_realtime(frame))
            self._listening = True
            while self._running and self._live_running:
                if time.monotonic() >= next_mode_check:
                    current_mode = read_voice_mode()
                    if current_mode != harness_voice:
                        logger.info("[live] voice mode changed; returning to VAD")
                        break
                    next_mode_check = time.monotonic() + 0.5
                now = time.time()
                if now - started > voice_cfg.LIVE_MAX_S:
                    logger.info("[live] session ceiling reached — hanging up")
                    break
                # K seconds with no action from the user — hang up and let the VAD watch
                # for the next one.

                unprompted = self._live_unprompted_replies
                if unprompted > voice_cfg.LIVE_MAX_UNPROMPTED_REPLIES:
                    logger.info(
                        "[live] %d replies with no user speech (device spoke "
                        "%d frame(s) meanwhile) — hanging up, VAD resumes",
                        unprompted,
                        self._live_frames_during_playback,
                    )
                    break
                if self._tts_is_speaking():
                    last_reply_end = now
                user_spoke_at = (
                    max(self._live_last_transcript_at, started)
                    if voice_cfg.LIVE_IDLE_REQUIRES_TRANSCRIPT
                    else last_user_speech
                )
                quiet_for = self._live_quiet_for(now, user_spoke_at, last_reply_end)
                pending_speech = self._live_idle_pending_speech(now, quiet_for, last_user_speech)
                if quiet_for > voice_cfg.LIVE_IDLE_HANGUP_S and not pending_speech:
                    logger.info(
                        "[live] no user action for %.0fs — hanging up, VAD resumes",
                        quiet_for,
                    )
                    break
                if self._live_hangup_at and now >= self._live_hangup_at:
                    logger.info(
                        "[live] farewell grace elapsed — hanging up, VAD resumes"
                    )
                    break
                if self._music_is_playing():
                    logger.info("[live] music started — hanging up")
                    break

                data, overflowed = mic.read(frame_size)
                if overflowed:
                    data = self._np.zeros_like(data)
                if getattr(self, "_live_gate", None) is not None:
                    raw_rms = rms(data, self._np)
                    input_samples = len(data)
                    # Pending synthesis is not audible playback. In particular,
                    # a TTS error/retry must never mute the user's microphone.
                    playback = self._tts_is_speaking() and live_playback.is_playing()
                    output_level = live_playback.level()
                    was_speaking = self._live_gate.speaking
                    was_ducked = self._live_gate.duck
                    played_seconds = max(0.0, live_playback.played_seconds() - self._aec_live_played_start)
                    data = self._live_gate.process(
                        data.reshape(-1), device_rate, playback, output_level,
                        playback_seconds=played_seconds,
                    )
                    gated = self._live_gate.risk and not self._live_gate.speaking
                    replay_ms = max(0, len(data) - input_samples) * 1000 / device_rate
                    self._live_frames_during_playback += int(playback)
                    self._live_frames_substituted += int(gated)
                    tick = time.monotonic()
                    if (tick >= self._aec_live_diag_next or replay_ms
                            or was_ducked != self._live_gate.duck):
                        self._aec_live_diag_next = tick + 1.0
                        logger.info(
                            "[live-aec] mic=%.0f out=%.0f threshold=%.0f noise=%.0f "
                            "echo_db=%.1f playback=%s realtime=%s speech=%s gate=%s "
                            "candidate=%s duck=%s prefix_ms=%.0f last_upload_ms=%.1f played_s=%.2f aec_ready=%s",
                            raw_rms, output_level * 32768, self._live_gate.threshold * 32768,
                            self._live_gate.noise * 32768, self._live_gate.coupling_db,
                            playback, bool(self._tts and self._tts.realtime_speaking),
                            self._live_gate.speaking, gated, self._live_gate.duck,
                            live_playback.snapshot()['duck'], replay_ms, self._aec_live_last_upload_ms,
                            played_seconds, played_seconds >= self._live_gate.AEC_WARMUP_S,
                        )
                    data = data.reshape(-1, 1)
                    live_playback.duck(self._live_gate.duck)
                    if self._live_gate.barge_in:
                        self._aec_live_interrupt_reply("local_speech")
                    if self._live_gate.speaking and not was_speaking:
                        logger.info("[live-aec] speech confirmed threshold=%.0f duck=%s",
                                    self._live_gate.threshold * 32768, self._live_gate.duck)
                else:
                    data = self._live_uplink_frame(data)
                self._live_frames += 1

                energy = rms(data, self._np)
                self._mic_level = raw_rms if getattr(self, "_live_gate", None) is not None else energy
                self._mic_level_ts = now
                # Feeds ONLY the idle-hangup clock. Hardware AEC uses its adaptive
                # speech state directly.
                if getattr(self, "_live_gate", None) is not None:
                    if self._live_gate.speaking:
                        last_user_speech = now
                        self._live_unprompted_replies = 0
                elif energy >= voice_cfg.RMS_THRESHOLD:
                    silence_probe.append(data)
                    if len(silence_probe) >= max(
                        1, voice_cfg.SILENCE_VAD_WINDOW_FRAMES
                    ):
                        window = self._np.concatenate(silence_probe)
                        silence_probe = []
                        if self._silence_window_is_speech(window, device_rate):
                            last_user_speech = now
                            self._live_unprompted_replies = 0

                # Expire emotion cues / recheck focus, never infer speech from silence.
                cues.tick()
                uplink_frame = resample_to_stt(
                    data, device_rate, voice_cfg.STT_RATE, self._np
                )
                if uplink_dump is not None:
                    uplink_dump.writeframes(uplink_frame)
                upload_started = time.monotonic()
                self._realtime.append_audio(self._to_realtime(uplink_frame))
                if getattr(self, "_live_gate", None) is not None:
                    self._aec_live_last_upload_ms = (time.monotonic() - upload_started) * 1000
        except Exception as e:
            logger.warning("[live] session error: %s", e)
        finally:
            cues.close()
            if uplink_dump is not None:
                try:
                    uplink_dump.close()
                except Exception:
                    pass
            if getattr(self, "_live_gate", None) is not None:
                live_playback.duck(False)
                self._live_gate.reset()
            self._live_running = False
            self._listening = False
            pump_stop.set()
            pump.join()
            self._realtime.end_live_audio()
            self._realtime.set_live_active(False)
            self._live_stop_output()
            logger.info(
                "[live] session END after %.0fs — %d frames, %d during playback, "
                "%d substituted",
                time.time() - started,
                self._live_frames,
                self._live_frames_during_playback,
                self._live_frames_substituted,
            )

        # Capture is corrupted by full-duplex playback on this codec and does NOT
        # recover on its own.
        return self._live_frames_during_playback > 0

    def _try_live_opener(self, mic, frame_size, device_rate, audio_buffer, *,
                         transcript, interaction_id, harness_voice,
                         voice_turn_type="voice", speaker_display=None):
        """Promote a confirmed STT capture; return (consumed, reopen_mic)."""
        if (not voice_cfg.LIVE_MODE or not hal_config.REALTIME_ENABLED
                or bypass_realtime(harness_voice) or not audio_buffer):
            return False, False
        opener = {"key": "", "consumed": False, "transcript": transcript,
                  "interaction_id": interaction_id, "voice_turn_type": voice_turn_type}
        try:
            self._realtime.prepare_turn()
            if not self._realtime.wait_until_available(5.0):
                return False, False
            self._realtime.send_text(build_turn_context(speaker_display))
            reopen = self._live_session(
                mic, frame_size, device_rate, audio_buffer,
                harness_voice=harness_voice, opener=opener,
            )
            return opener["consumed"], reopen
        except Exception:
            logger.exception("[live] confirmed opener failed; preserving STT fallback")
            self._live_running = False
            self._realtime.set_live_active(False)
            self._live_stop_output()
            return opener["consumed"], True

    def _to_realtime(self, pcm16_bytes: bytes):
        """16 kHz PCM16 bytes → float32 at the provider's input rate."""
        audio_f32 = pcm16_bytes_to_float32(pcm16_bytes)
        dst = self._realtime.sample_rate
        rs = getattr(self, "_uplink_resampler", None)
        if rs is None or rs.src_rate != voice_cfg.STT_RATE or rs.dst_rate != dst:
            rs = StreamingResampler(voice_cfg.STT_RATE, dst)
            self._uplink_resampler = rs
        return rs.process(audio_f32)

    def cancel_automatic_reply(self):
        """Release the turn-based receive loop when the user takes the floor."""
        with self._automatic_reply_lock:
            stop = self._automatic_reply_stop
            if stop is None:
                return False
            if not stop.is_set():
                self._automatic_reply_cancelled_at = time.monotonic()
                stop.set()
                logger.info("[automic-stop] reply cancellation requested")
            return True

    def _run_automatic_realtime_turn(self, reply_stop, *args, **kwargs):
        with self._automatic_reply_lock:
            self._automatic_reply_stop = reply_stop
        return run_realtime_turn(
            *args, **kwargs, stop_event=reply_stop, background_cancel_recovery=True,
        )

    def _stream_session(
        self, mic, frame_size: int, device_rate: int,
        preconnected_session=None, speech_pre_buffer=None,
        pending_listening_cue_id=None, harness_voice=None, manual_capture=None,
        wake_focus_at_entry=None,
    ):
        tts = self._tts
        reserve = getattr(tts, "begin_input_capture", None)
        token = reserve() if reserve and not voice_cfg.LIVE_MODE and manual_capture is None else None

        def release_input():
            if token is not None:
                tts.end_input_capture(token)

        followup_ids = set()
        reply_stop = threading.Event()
        try:
            return VoiceService._stream_session_impl(
                self, mic, frame_size, device_rate, preconnected_session,
                speech_pre_buffer, pending_listening_cue_id, harness_voice,
                manual_capture, release_input, followup_ids,
                wake_focus_at_entry, reply_stop,
            )
        finally:
            with self._automatic_reply_lock:
                if self._automatic_reply_stop is reply_stop:
                    self._automatic_reply_stop = None
            release_input()
            for iid in followup_ids:
                self._wakeword_focus.finish(iid, cancelled=reply_stop.is_set())
            if reply_stop.is_set():
                from hal import app_state

                app_state.clear_listening_cue()
            # A capture dropped as noise (or routed without a realtime reply) never
            # reaches stream_output, which is what normally ends the realtime turn.
            realtime = getattr(self, "_realtime", None)
            if realtime is not None:
                realtime.finish_capture()

    def _stream_session_impl(
        self,
        mic,
        frame_size: int,
        device_rate: int,
        preconnected_session=None,
        speech_pre_buffer=None,
        pending_listening_cue_id=None,
        harness_voice=None,
        manual_capture=None,
        release_input=lambda: None,
        followup_ids=None,
        wake_focus_at_entry=None,
        reply_stop=None,
    ):
        """Stream audio to STT provider until silence or TTS interrupts."""
        harness_voice = read_voice_mode() if harness_voice is None else harness_voice
        if requires_manual_capture(harness_voice) and manual_capture is None:
            # A mode toggle can race the last normal VAD frame. Manual input
            # must always have an explicit tap-owned capture.
            if preconnected_session is not None:
                preconnected_session.close()
            return
        # Live providers use automatic endpointing for the whole process. A gated
        # opener/fallback must not flush a buffered utterance through the manual commit
        # path (which can double-commit with server VAD).
        input_policy = InputPolicy.for_turn(harness_voice, manual_capture, live_mode=voice_cfg.LIVE_MODE)
        realtime_allowed = input_policy.realtime_allowed
        # Harness explicitly owns voice input for this capture. Do not extend
        # the normal wake window; disabling the mode restores its usual gate.
        harness_listening = input_policy.harness_capture
        if preconnected_session is not None and preconnected_session.is_closed():
            logger.warning(
                "STT keepalive: pre-connected session went stale (idle close) — "
                "connecting a fresh session for this turn"
            )
            try:
                preconnected_session.close()
            except Exception:
                pass
            preconnected_session = None

        stt_session = preconnected_session or self._stt.create_session()
        # Latch focus at session start. A user who began speaking before the
        # deadline may finish their sentence after it, but a later session must
        # use the wake phrase again.
        wakeword_followup_active = (
            hal_config.WAKEWORD_ENABLED and self._wakeword_focus.is_active()
        )
        if wake_focus_at_entry is None:
            wake_focus_at_entry = wakeword_followup_active
        # Freeze at entry: later wake phrases/gaze refreshes or expiry cannot
        # promote a follow-up into an audible opener. LEDs and routing stay separate.
        suppress_auto_fillers = bool(
            hal_config.VOICE_OPENING_FILLERS_ONLY
            and input_policy.automatic and not harness_listening
            and not voice_cfg.LIVE_MODE and hal_config.WAKEWORD_ENABLED
            and wake_focus_at_entry
        )
        if wakeword_followup_active:
            logger.info("Wake-word follow-up focus accepted for this session")

        last_partial = [""]
        final_segments = []
        stt_final_changed = threading.Event()
        final_sent = [False]
        final_ts = [0.0]
        turn_endpoint = None
        if voice_cfg.TURN_END_ENABLED and not voice_cfg.LIVE_MODE and manual_capture is None:
            turn_endpoint = TurnEndpoint(
                self._turn_detector,
                fallback_s=voice_cfg.TURN_END_FALLBACK_S,
                max_pause_s=voice_cfg.TURN_END_MAX_PAUSE_S,
            )
        # The listening cue fires on the FIRST STT PARTIAL — never at session open.
        listening_emotion_sent = [False]
        audio_buffer: list[bytes] = []
        last_speech_idx: int = -1
        endpoint_method = "stt_error"
        endpoint_ts = 0.0
        early_realtime_result = None
        interaction_id = None
        session_start = time.time()
        followup_ids = set() if followup_ids is None else followup_ids

        def hold_followup():
            if (input_policy.automatic and hal_config.WAKEWORD_ENABLED and not harness_listening
                    and (wake_word_confirmed.is_set() or wakeword_followup_active
                         or self._wakeword_focus.is_active())
                    and self._wakeword_focus.begin(interaction_id)):
                followup_ids.add(interaction_id)
        gaze_endpoint_checked = False
        pre_frames_from_vad = len(speech_pre_buffer or [])
        logger.info(
            "Session START — pre_from_vad=%d frames, device_rate=%dHz",
            pre_frames_from_vad,
            device_rate,
        )
        if realtime_allowed and hal_config.REALTIME_ENABLED and hal_config.WAKEWORD_ENABLED:
            self._realtime.prewarm()
        wake_word_detected = threading.Event()
        wake_word_confirmed = threading.Event()
        capture_complete = threading.Event()
        wake_partial_hypothesis = [""]
        wake_final_hypothesis = [""]

        def wake_partial_candidate(text: str) -> str:
            wake_partial_hypothesis[0] = merge_stt_hypothesis(
                wake_partial_hypothesis[0], text
            )
            return wake_partial_hypothesis[0]

        def wake_final_candidate(text: str) -> str:
            # Do not merge an interim hypothesis into the final one. That would
            # preserve a corrected false-positive wake word indefinitely.
            wake_final_hypothesis[0] = merge_stt_hypothesis(
                wake_final_hypothesis[0], text
            )
            wake_partial_hypothesis[0] = ""
            return wake_final_hypothesis[0]

        def addressed_to_us() -> bool:
            """Whether the sentence being spoken has been shown to be for us."""
            return harness_listening or is_addressed(
                hal_config.WAKEWORD_ENABLED,
                wake_word_detected.is_set(),
                wakeword_followup_active,
                self._wakeword_focus.is_active(),
            )

        # Gaze is read once, at speech start: the pre-speech window is the evidence.
        facing_at_start = facing_evidence()

        def addressed_evidence() -> bool:
            """Whether anything beyond the audio says this speech was for the device."""
            return bool(
                wake_word_detected.is_set()
                or wakeword_followup_active or self._wakeword_focus.is_active()
                or short_answer_expected()
                or facing_at_start
                or turn_speaker_display
            )

        def turn_context() -> str:
            """The per-turn context, with what the device knows about who is talking to it."""
            hint = addressed_hint(
                wake_word=wake_word_detected.is_set(),
                window=bool(wakeword_followup_active or self._wakeword_focus.is_active()),
                question=short_answer_expected(),
                facing=facing_at_start,
                known_voice=turn_speaker_display or "",
            )
            logger.info("[admission] evidence: %s", hint)
            if hal_config.ADDRESSED_GATE == "off":
                return build_turn_context(turn_speaker_display)
            return build_turn_context(turn_speaker_display, addressed=hint)

        # Strict gate: hands-free speech with no evidence never reaches a model.
        # Only when the wake word is off (with it on, the wake gate already decides).
        strict_gate = (
            strict_addressed_gate() and not hal_config.WAKEWORD_ENABLED
            and input_policy.automatic and not harness_listening and manual_capture is None
        )

        def fire_listening_cue() -> None:
            """Show the listening cue, once per session, only when this turn is
            actually addressed to the device.
            """
            if capture_complete.is_set():
                return
            if manual_capture is not None:
                return
            if listening_emotion_sent[0]:
                return
            if not addressed_to_us():
                return
            listening_emotion_sent[0] = True
            self._set_emotion_local(presets.EMO_LISTENING)

        def open_wake_word_gate(candidate: str, source: str) -> None:
            if (
                hal_config.WAKEWORD_ENABLED
                and candidate
                and self._decorator.starts_with_wake_word(candidate)
                and not wake_word_detected.is_set()
            ):
                wake_word_detected.set()
                logger.info(
                    "Wake-word gate opened by STT %s: '%s'", source, candidate
                )
                fire_listening_cue()

        def confirm_wake_word_gate(candidate: str) -> None:
            if (
                hal_config.WAKEWORD_ENABLED
                and candidate
                and self._decorator.starts_with_wake_word(candidate)
            ):
                wake_word_confirmed.set()
                open_wake_word_gate(candidate, "final")
                logger.info("Wake-word gate confirmed by STT final: '%s'", candidate)

        def on_transcript(text: str, is_final: bool):
            if reply_stop is not None and reply_stop.is_set():
                return
            if text.strip():
                self._last_transcript_ts = time.time()
            if not is_final:
                logger.info("STT partial: '%s'", text)
                candidate = wake_partial_candidate(text)
                if hal_config.WAKEWORD_ENABLED:
                    logger.debug("Wake-word partial candidate: '%s'", candidate)
                    open_wake_word_gate(candidate, "partial")
                last_partial[0] = text
                # Same gate as the listening cue below. A backchannel is the device
                # saying "go on, I'm listening", which is a claim to be the addressee —
                # so it must not fire for a sentence the device has not been shown is
                # meant for it.
                if (input_policy.automatic and not suppress_auto_fillers
                        and not capture_complete.is_set() and addressed_to_us()):
                    self._backchannel.on_partial(text)
                fire_listening_cue()
                return
            logger.info("STT final segment: '%s'", text)
            if hal_config.WAKEWORD_ENABLED:
                confirm_wake_word_gate(wake_final_candidate(text))
            prev = last_partial[0]
            if (
                prev
                and len(text) < len(prev)
                and SequenceMatcher(None, prev.lower(), text.lower()).ratio()
                < _TRANSCRIPT_MIN_SIMILARITY
            ):
                segments = [prev, text]
            else:
                segments = [text]
            for seg in segments:
                if seg:
                    final_segments.append(seg)
            last_partial[0] = ""
            final_sent[0] = True
            final_ts[0] = time.time()
            stt_final_changed.set()

        rt_audio_buffer: list = []
        realtime_deferred = False
        realtime_turn_started = False
        realtime_start_failed = False
        audio_turn = None
        prepare_started = False
        prepare_endpoint_waited = False
        prepare_done = threading.Event()
        prepare_error = []
        post_capture_wait_filler = None
        turn_identity = None
        turn_speaker_display = None
        sent_turn_speaker = None
        turn_context_sent = False

        # Give backchannel cues a lifecycle token before any STT callback can
        # schedule one. A cue delayed behind normal TTS must not survive this
        # capture and play into the next mic session.
        self._backchannel.begin_session()

        def prepare_capture_session() -> bool:
            """Connect off the mic thread; retain audio until binding is ready."""
            nonlocal prepare_started, prepare_endpoint_waited
            if not prepare_started:
                prepare_started = True

                def prepare():
                    try:
                        self._realtime.prepare_turn()
                    except Exception as error:
                        prepare_error.append(error)
                    finally:
                        prepare_done.set()

                threading.Thread(target=prepare, daemon=True, name="rt-capture-prepare").start()
            if capture_complete.is_set() and not prepare_endpoint_waited:
                prepare_endpoint_waited = True
                prepare_done.wait(timeout=PREWARM_JOIN_TIMEOUT_S)
            return prepare_done.is_set() and not prepare_error

        def start_realtime_turn() -> bool:
            """Open realtime as soon as the wake-word partial is available."""
            nonlocal realtime_deferred, realtime_turn_started, realtime_start_failed
            nonlocal sent_turn_speaker, turn_context_sent
            nonlocal wakeword_followup_active
            nonlocal audio_turn
            if not realtime_allowed or realtime_turn_started or realtime_start_failed:
                return False

            if hal_config.WAKEWORD_ENABLED:
                wakeword_followup_active = (
                    wakeword_followup_active or self._wakeword_focus.is_active()
                )
                if (
                    (not capture_complete.is_set() and not wakeword_followup_active)
                    or not (wake_word_confirmed.is_set() or wakeword_followup_active)
                    or not hal_config.REALTIME_ENABLED
                ):
                    return False
                if not prepare_capture_session():
                    return False
                if self._realtime.rebuilding or not self._realtime.available:
                    logger.info(
                        "[realtime] Wake-word turn falls back — session unavailable after final confirmation"
                    )
                    return False
                try:
                    audio_turn = self._realtime.bind_audio_turn()
                    self._realtime.send_text(turn_context())
                    sent_turn_speaker = turn_speaker_display
                    turn_context_sent = True
                    for audio_f32 in rt_audio_buffer:
                        self._realtime.append_audio(audio_f32, turn=audio_turn)
                    realtime_turn_started = True
                    logger.info(
                        "[realtime] Wake-word/follow-up authorized; flushed %d buffered frame(s)",
                        len(rt_audio_buffer),
                    )
                    return True
                except AudioTurnSessionChanged:
                    realtime_turn_started = True
                    realtime_deferred = True
                    logger.info("[realtime] Upload session changed; retaining full turn for replay")
                    return False
                except Exception as e:
                    realtime_start_failed = True
                    logger.warning(
                        "[realtime] Wake-word start failed; forwarding final STT to main agent: %s",
                        e,
                    )
                    return False

            if not hal_config.REALTIME_ENABLED:
                realtime_turn_started = True
                return True

            if not prepare_capture_session():
                return False
            realtime_turn_started = True
            realtime_deferred = self._realtime.rebuilding
            if not realtime_deferred and self._realtime.available:
                try:
                    audio_turn = self._realtime.bind_audio_turn()
                    self._realtime.send_text(turn_context())
                    sent_turn_speaker = turn_speaker_display
                    turn_context_sent = True
                    for audio_f32 in rt_audio_buffer:
                        self._realtime.append_audio(audio_f32, turn=audio_turn)
                    if hal_config.WAKEWORD_ENABLED:
                        logger.info(
                            "[realtime] Wake-word gate opened; flushed %d buffered frame(s)",
                            len(rt_audio_buffer),
                        )
                except AudioTurnSessionChanged:
                    realtime_deferred = True
                    logger.info("[realtime] Upload session changed; retaining full turn for replay")
                except Exception as e:
                    logger.warning("[realtime] start turn failed: %s", e)
                    realtime_start_failed = True
                    realtime_turn_started = False
            return True
        try:
            if preconnected_session:
                stt_session._on_transcript_cb = on_transcript
                logger.info("STT keepalive: reusing pre-connected session")

            connect_ok = [False]
            connect_done = threading.Event()

            def _do_connect():
                try:
                    connect_ok[0] = stt_session.start(on_transcript)
                finally:
                    if manual_capture is not None and (
                        manual_capture.cancelled.is_set() or not self._running
                    ):
                        stt_session.close()
                        connect_ok[0] = False
                    connect_done.set()

            if preconnected_session:
                connect_ok[0] = True
                connect_done.set()
            else:
                threading.Thread(
                    target=_do_connect, daemon=True, name="stt-connect"
                ).start()

            pre_buffer = []
            connect_started = time.monotonic()
            while not connect_done.wait(timeout=0.005):
                if manual_capture is not None and (
                    not self._running or manual_capture.cancelled.is_set()
                    or manual_capture.finished.is_set()
                    or time.monotonic() - connect_started > 10
                ):
                    manual_capture.cancelled.set()
                    break
                if self._tts_is_speaking():
                    connect_done.wait(timeout=2)
                    break
                data, overflowed = mic.read(frame_size)
                if not overflowed:
                    pre_buffer.append(
                        resample_to_stt(data, device_rate, voice_cfg.STT_RATE, self._np)
                    )

            if not connect_ok[0]:
                return
            if manual_capture is not None:
                if manual_capture.cancelled.is_set() or manual_capture.finished.is_set():
                    return
                input_policy.set_capturing(True, self._set_emotion_local)
                if self._tts:
                    cue_start = time.monotonic()
                    self._tts.play_harness_capture_chime()
                    cue_frames = int((time.monotonic() - cue_start) * device_rate / frame_size) + 1
                    for _ in range(cue_frames):
                        mic.read(frame_size)
                pre_buffer.clear()

            start_realtime_turn()

            all_pre = (speech_pre_buffer or []) + pre_buffer
            if all_pre:
                logger.info(
                    "Session FILL (pre-flush) — added %d frames (~%.0fms) to buffer",
                    len(all_pre),
                    len(all_pre) * voice_cfg.FRAME_DURATION_MS,
                )
                used_preconnected = preconnected_session is not None

                def _send_pre_roll():
                    if stt_session.is_closed():
                        raise RuntimeError("pre-connected STT session is closed")
                    for pre_frame in all_pre:
                        stt_session.send_audio(pre_frame)

                try:
                    _send_pre_roll()
                except Exception as e:
                    if not used_preconnected:
                        raise
                    logger.warning(
                        "STT keepalive closed at speech start (%s) — reconnecting "
                        "and replaying %d pre-roll frame(s)",
                        "normal 1000 close" if _is_normal_ws_close(e) else str(e),
                        len(all_pre),
                    )
                    try:
                        stt_session.close()
                    except Exception:
                        pass
                    stt_session = self._stt.create_session()
                    if not stt_session.start(on_transcript):
                        raise RuntimeError("fresh STT session failed to connect") from e
                    _send_pre_roll()

                for frame in all_pre:
                    audio_buffer.append(frame)
                    if realtime_allowed and hal_config.REALTIME_ENABLED:
                        audio_f32 = pcm16_bytes_to_float32(frame)
                        audio_f32 = resample_float32(
                            audio_f32, voice_cfg.STT_RATE, self._realtime.sample_rate
                        )
                        rt_audio_buffer.append(audio_f32)
                        if (
                            realtime_turn_started
                            and not realtime_deferred
                            and self._realtime.available
                        ):
                            try:
                                self._realtime.append_audio(audio_f32, turn=audio_turn)
                            except AudioTurnSessionChanged:
                                realtime_deferred = True
                                logger.info("[realtime] Pre-roll session changed; retaining full turn for replay")

                start_realtime_turn()

            self._listening = True
            last_speech_time = time.time()
            session_start = time.time()
            last_speech_idx = len(audio_buffer) - 1
            silence_probe: list = []
            silence_vad_on = (
                voice_cfg.SILENCE_VAD_ENABLED
                and voice_cfg.SILENCE_VAD_WINDOW_FRAMES > 0
            )
            if silence_vad_on and self._silence_vad is not None:
                self._silence_vad.reset_state()
            noise_windows = 0
            try:
                requests.post(
                    "http://127.0.0.1:5000/api/sensing/event",
                    json={"type": "voice_listening", "message": "listening"},
                    timeout=0.3,
                )
            except Exception:
                pass

            mode_checked_at = 0.0
            while self._running and not stt_session.is_closed():
                if manual_capture is not None:
                    if time.monotonic() - mode_checked_at >= 0.25:
                        mode_checked_at = time.monotonic()
                        current_mode = read_voice_mode()
                        self.device_input.observe(current_mode)
                        if not same_capture_target(harness_voice, current_mode):
                            manual_capture.cancelled.set()
                    if manual_capture.cancelled.is_set():
                        break
                    if manual_capture.finished.is_set():
                        endpoint_method = "manual_tap"
                        endpoint_ts = time.monotonic()
                        break
                # Only the capture thread opens and flushes the realtime activity;
                # The STT callback merely latches wake_word_detected.
                start_realtime_turn()
                if self._tts_is_speaking():
                    logger.info("TTS started mid-session, closing STT to avoid echo")
                    endpoint_method = "tts_started"
                    endpoint_ts = time.monotonic()
                    break
                if self._music_is_playing():
                    logger.info("Music started mid-session, closing STT")
                    endpoint_method = "music_started"
                    endpoint_ts = time.monotonic()
                    break

                has_words = any(c.isalnum() for c in last_partial[0]) or any(
                    any(c.isalnum() for c in segment) for segment in final_segments
                )
                duration_limit = (
                    voice_cfg.TURN_END_MAX_DURATION_S
                    if turn_endpoint is not None and has_words
                    else voice_cfg.MAX_SESSION_DURATION_S
                )
                if (time.time() - session_start) > duration_limit:
                    logger.warning(
                        "STT session exceeded %ds, force-closing",
                        duration_limit,
                    )
                    endpoint_method = "max_duration"
                    endpoint_ts = time.monotonic()
                    break

                data, overflowed = mic.read(frame_size)
                if overflowed:
                    continue

                resampled = resample_to_stt(data, device_rate, voice_cfg.STT_RATE, self._np)
                try:
                    stt_session.send_audio(resampled)
                except Exception as e:
                    logger.warning("send_audio failed (connection dead?): %s", e)
                    endpoint_method = "stt_error"
                    endpoint_ts = time.monotonic()
                    break
                audio_buffer.append(resampled)

                # Parallel: stream to realtime model (non-blocking queue put).
                # During a pending noise-drop rebuild retain frames locally and
                # flush them once to the clean replacement session below.
                if realtime_allowed and hal_config.REALTIME_ENABLED:
                    audio_f32 = pcm16_bytes_to_float32(resampled)
                    audio_f32 = resample_float32(
                        audio_f32, voice_cfg.STT_RATE, self._realtime.sample_rate
                    )
                    rt_audio_buffer.append(audio_f32)
                    opened_now = start_realtime_turn()
                    if (
                        realtime_turn_started
                        and not opened_now
                        and not realtime_deferred
                        and self._realtime.available
                    ):
                        try:
                            self._realtime.append_audio(audio_f32, turn=audio_turn)
                        except AudioTurnSessionChanged:
                            realtime_deferred = True

                energy = rms(data, self._np)
                self._mic_level = energy
                self._mic_level_ts = time.time()
                endpoint_candidate = energy < voice_cfg.RMS_THRESHOLD
                if energy >= voice_cfg.RMS_THRESHOLD:
                    if not silence_vad_on:
                        last_speech_time = time.time()
                        last_speech_idx = len(audio_buffer) - 1
                    else:
                        silence_probe.append(data)
                        if len(silence_probe) >= voice_cfg.SILENCE_VAD_WINDOW_FRAMES:
                            window = self._np.concatenate(silence_probe)
                            probe_frames = len(silence_probe)
                            silence_probe = []
                            if self._silence_window_is_speech(window, device_rate):
                                last_speech_time = time.time()
                                last_speech_idx = len(audio_buffer) - 1
                            else:
                                # Loud background noise must not suppress the
                                # endpoint clock. Only admit a completed Silero
                                # rejection, never an unclassified loud window.
                                endpoint_candidate = True
                                noise_windows += 1
                                if noise_windows in (1, 10, 50):
                                    logger.info(
                                        "Silence clock: %d loud window(s) rejected as "
                                        "non-speech (%d frames each)",
                                        noise_windows, probe_frames,
                                    )
                if endpoint_candidate and manual_capture is None and turn_should_close(time.time(), last_speech_time, final_ts[0]):
                    if turn_endpoint is not None:
                        end = len(audio_buffer)
                        start = max(0, end - 125)
                        if not turn_endpoint.should_close(
                            now=time.time(), last_speech=last_speech_time,
                            final_at=final_ts[0],
                            text=" ".join([*final_segments, last_partial[0]]).strip(),
                            pcm=b"".join(audio_buffer[start:end]),
                        ):
                            continue
                    if noise_windows:
                        logger.info(
                            "Silence detected, disconnecting STT "
                            "(%d loud window(s) were non-speech)", noise_windows
                        )
                    else:
                        logger.info("Silence detected, disconnecting STT")
                    endpoint_method = turn_endpoint.reason if turn_endpoint else "silence_clock"
                    endpoint_ts = time.monotonic()
                    break
        except Exception as e:
            if _is_normal_ws_close(e):
                logger.warning(
                    "STT session closed normally (1000) before turn completion; "
                    "audio for this turn was discarded"
                )
            else:
                logger.error("STT stream error: %s", e)
        finally:
            self._backchannel.reset()
            self._listening = False
            release_input()
            input_policy.set_capturing(False, self._set_emotion_local)
            # A confirmed transcript can arrive before CloseStream drains. For
            # already-authorized input, overlap that drain with the model reply.
            # Do not use a provisional partial to bypass the existing noise gate.
            # Only a reply that just ended can leak into this capture as an echo prefix.
            capture_spoken_text = recent_spoken_text(self._tts, now=session_start)
            early_words, early_duration = "", 0.0
            if realtime_allowed and hal_config.REALTIME_ENABLED and manual_capture is None:
                early_words, _, early_duration = finalize_session(
                    list(audio_buffer), [""], list(final_segments), last_speech_idx,
                    capture_spoken_text,
                )
            if early_words and hal_config.WAKEWORD_ENABLED:
                try:
                    from hal.drivers.tracking import gaze

                    gaze.on_speech_end()
                    gaze_endpoint_checked = True
                except Exception as e:
                    logger.debug("early gaze speech-end check skipped: %s", e)
                wakeword_followup_active = (
                    wakeword_followup_active or self._wakeword_focus.is_active()
                )
            early_authorized = not hal_config.WAKEWORD_ENABLED or wakeword_followup_active
            overlap_drain = (
                realtime_allowed and hal_config.REALTIME_ENABLED and self._running
                and manual_capture is None and early_authorized
                and (self._stt_drain_future is None or self._stt_drain_future.done())
                and not harness_followup_active()
                and endpoint_method in {"smart_turn", "turn_fallback", "turn_pause_limit", "silence_clock"}
            )
            if overlap_drain:
                from concurrent.futures import ThreadPoolExecutor, TimeoutError as DrainTimeout

                capture_complete.set()
                drain_finished = threading.Event()

                def close_stt():
                    try:
                        stt_session.close()
                    finally:
                        drain_finished.set()
                        stt_final_changed.set()

                if self._stt_drain_worker is None:
                    self._stt_drain_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt-final-drain")
                final_drain = self._stt_drain_worker.submit(close_stt)
                self._stt_drain_future = final_drain
                try:
                    last_final_snapshot = None
                    while self._running:
                        stt_final_changed.clear()
                        # A long partial is the user's words, not an STT fabrication
                        # over noise: commit on it instead of waiting for the final.
                        partial_words = last_partial[0] if not final_segments and confident_partial(last_partial[0]) else ""
                        final_snapshot = (tuple(final_segments), partial_words)
                        if final_snapshot == last_final_snapshot:
                            if drain_finished.is_set():
                                break
                            stt_final_changed.wait(timeout=0.1)
                            continue
                        last_final_snapshot = final_snapshot
                        early_words, _, early_duration = finalize_session(
                            list(audio_buffer), [partial_words], list(final_snapshot[0]), last_speech_idx,
                            capture_spoken_text,
                        )
                        early_speech = True
                        if (early_words and needs_noise_guard(early_words)
                                and hal_config.REALTIME_REQUIRE_SPEECH_ON_EMPTY_STT and audio_buffer):
                            try:
                                early_speech = self._rt_noise_is_speech(
                                    self._np.frombuffer(b"".join(audio_buffer), dtype=self._np.int16)
                                )
                            except Exception as e:
                                logger.warning("Early realtime speech check failed: %s", e)
                        if (early_words and not is_noise_turn(early_words, early_duration, early_speech)
                                and not (strict_gate and not addressed_evidence())):
                            interaction_id = voice_metrics.speech_end(endpoint_method, at=endpoint_ts)
                            hold_followup()
                            start_realtime_turn()
                            if realtime_turn_started and not realtime_deferred and self._running:
                                # Preparation may fail or outlast the filler timer.
                                # Only acknowledge a turn admitted to realtime.
                                if turn_context_sent and self._realtime.available:
                                    post_capture_wait_filler = _WaitFiller(owner=interaction_id)
                                    if not suppress_auto_fillers and should_arm_realtime_wait_filler(early_words):
                                        post_capture_wait_filler.arm()
                                logger.info("[realtime] Processing confirmed speech while STT final drain runs")
                                early_realtime_result = VoiceService._run_automatic_realtime_turn(self, reply_stop,
                                    self._realtime, self._tts, self.strip_rt_markers,
                                    early_words, rt_audio_buffer, early_duration, early_speech,
                                    interaction_id=interaction_id, save_history=False, audio_turn=audio_turn,
                                    harness_followup=False, wait_filler=post_capture_wait_filler,
                                    suppress_auto_fillers=suppress_auto_fillers,
                                )
                            break
                        if drain_finished.is_set():
                            break
                        stt_final_changed.wait(timeout=0.1)
                finally:
                    try:
                        while not reply_stop.is_set():
                            try:
                                final_drain.result(timeout=0.1)
                                break
                            except DrainTimeout:
                                if final_drain.done():
                                    raise
                                continue
                    except Exception:
                        if post_capture_wait_filler is not None:
                            post_capture_wait_filler.cancel()
                        raise
                if not self._running or reply_stop.is_set():
                    if post_capture_wait_filler is not None:
                        post_capture_wait_filler.cancel()
                    if pending_listening_cue_id is not None:
                        from hal import app_state

                        app_state.clear_listening_pending_cue(pending_listening_cue_id)
                    return
            else:
                stt_session.close()
            if pending_listening_cue_id is not None:
                from hal import app_state

                app_state.clear_listening_pending_cue(pending_listening_cue_id)
            combined, ser_audio_buffer, buf_duration = finalize_session(
                audio_buffer,
                last_partial,
                final_segments,
                last_speech_idx,
                capture_spoken_text,
            )
            capture_complete.set()
            if turn_endpoint is not None and endpoint_method == "max_duration":
                logger.warning("Hands-free capture limit reached; unfinished request discarded")
                if realtime_turn_started and hal_config.REALTIME_ENABLED:
                    self._realtime.discard_open_activity("max-duration")
                return
            if manual_capture is not None and (
                not self._running
                or endpoint_method != "manual_tap"
                or manual_capture.cancelled.is_set()
                or not same_capture_target(harness_voice, read_voice_mode())
            ):
                logger.info("Harness manual capture discarded without a valid finish tap")
                combined = ""
                harness_listening = False
            elif manual_capture is not None and self._tts:
                self._tts.play_harness_capture_chime(finished=True)
            if interaction_id is None:
                interaction_id = voice_metrics.speech_end(endpoint_method, at=endpoint_ts)
            logger.info(
                "Session END — buffer frames=%d bytes=%d duration=%.2fs "
                "transcript=%r interaction_id=%s",
                len(audio_buffer), sum(len(b) for b in audio_buffer), buf_duration,
                combined or "(empty)", interaction_id,
            )
            if (
                hal_config.WAKEWORD_ENABLED
                and wake_word_detected.is_set()
                and not wake_word_confirmed.is_set()
            ):
                if self._decorator.starts_with_wake_word(combined):
                    wake_word_confirmed.set()
                    logger.info(
                        "Wake-word confirmed on assembled transcript: %r", combined
                    )
                elif self._decorator.matches_wake_word_loosely(combined):
                    # STT rewrote its own hypothesis: the partial that opened the gate
                    # had the name right and the final came back with one letter
                    # changed.
                    wake_word_confirmed.set()
                    logger.info(
                        "Wake-word confirmed with a one-letter STT slip: %r "
                        "(exact match failed; the partial that opened the gate "
                        "had the name right)",
                        combined,
                    )
                else:
                    logger.info(
                        "Wake-word partial rejected — no matching final STT result; dropping turn"
                    )

            # A VAD-confirmed utterance can begin while the camera is pointed away from
            # the remembered user.
            if combined:
                try:
                    from hal.drivers.tracking import gaze

                    if not gaze_endpoint_checked:
                        gaze.on_speech_end()
                except Exception as e:
                    logger.debug("gaze speech-end check skipped: %s", e)
            else:
                # No transcript, but the reacquire at speech START already took the body
                # — and most sessions the wide entry VAD opens end exactly here.
                try:
                    from hal.drivers.tracking import gaze

                    gaze.release_reacquire_hold_if_pending()
                except Exception as e:
                    logger.debug("gaze reacquire release skipped: %s", e)
            if reply_stop is not None and reply_stop.is_set():
                self._wakeword_focus.finish(interaction_id, cancelled=True)
                return False
            wakeword_followup_active = (
                wakeword_followup_active
                or (hal_config.WAKEWORD_ENABLED and self._wakeword_focus.is_active())
            )

            # Noise guard: a session can open on a noise blip that fools the entry VAD,
            # and STT then either finds no words or invents a short filler for it.
            rt_audio_is_speech = True
            if (
                needs_noise_guard(combined)
                and hal_config.REALTIME_REQUIRE_SPEECH_ON_EMPTY_STT
                and audio_buffer
            ):
                try:
                    pcm = self._np.frombuffer(
                        b"".join(audio_buffer), dtype=self._np.int16
                    )
                    rt_audio_is_speech = self._rt_noise_is_speech(pcm)
                    logger.info(
                        "[realtime] noise-guard ran: stt=%r, silero_speech=%s "
                        "(samples=%d, dur=%.2fs)",
                        combined[:40] if combined else "(empty)",
                        rt_audio_is_speech, len(pcm), buf_duration,
                    )
                except Exception as e:
                    logger.warning("Realtime noise-guard buffer decode failed: %s", e)

            defer_speaker_prepass = should_defer_speaker_id_prepass(combined)

            def resolve_turn_speaker_identity(*, after_realtime_decision: bool = False) -> None:
                nonlocal turn_identity, turn_speaker_display
                if not combined:
                    return
                _final_text, _ = self._decorator.classify_wake_word(combined)
                try:
                    turn_identity = self._decorator.identify_and_decorate(
                        _final_text, audio_buffer, in_followup=wakeword_followup_active
                    )
                    turn_speaker_display = turn_identity[2]
                    if turn_speaker_display and turn_identity[1]:
                        from hal import app_state as _identity_state

                        _identity_state.set_voice_user(
                            turn_identity[1], turn_speaker_display
                        )
                except Exception as e:
                    logger.warning("[realtime] speaker-ID prepass failed: %s", e)
                logger.info(
                    "[realtime] speaker-ID prepass: display=%r (se_user=%r) — "
                    "context already sent with speaker=%r → %s",
                    turn_speaker_display,
                    turn_identity[1] if turn_identity else None,
                    sent_turn_speaker,
                    (
                        "resolved after realtime decision"
                        if after_realtime_decision
                        else (
                            "correction needed"
                            if (
                                turn_speaker_display
                                and turn_speaker_display != sent_turn_speaker
                            )
                            else "no correction needed"
                        )
                    ),
                )

            speaker_prepass_thread = None
            if defer_speaker_prepass:
                logger.info(
                    "[realtime] Short transcript — deferring speaker-ID prepass "
                    "until after the AI rejection decision"
                )
            else:
                speaker_prepass_thread = threading.Thread(
                    target=resolve_turn_speaker_identity,
                    daemon=True,
                    name="speaker-id-prepass",
                )
                speaker_prepass_thread.start()

            def join_speaker_prepass(wait_s: float = voice_cfg.SPEAKER_PREPASS_JOIN_S) -> None:
                """Wait within the commit or downstream identity budget."""
                if speaker_prepass_thread is None or not speaker_prepass_thread.is_alive():
                    return
                waited_from = time.time()
                speaker_prepass_thread.join(max(0.0, wait_s))
                logger.info(
                    "[realtime] waited %.2fs for speaker-ID prepass (resolved=%s)",
                    time.time() - waited_from,
                    not speaker_prepass_thread.is_alive(),
                )

            strict_rejected = bool(
                strict_gate and combined
                and not is_noise_turn(combined, buf_duration, rt_audio_is_speech)
                and not addressed_evidence()
            )
            if strict_rejected:
                logger.info(
                    "[admission] strict gate: no evidence the speech was for the device "
                    "(stt=%r) — not sent to any model", combined[:60],
                )
                if realtime_turn_started and hal_config.REALTIME_ENABLED:
                    try:
                        self._realtime.discard_open_activity("not-addressed")
                    except Exception:
                        logger.exception("[admission] discard after strict rejection failed")
                if post_capture_wait_filler is not None:
                    post_capture_wait_filler.cancel()
                    post_capture_wait_filler = None
            if strict_rejected:
                pass
            elif is_noise_turn(combined, buf_duration, rt_audio_is_speech):
                if not realtime_turn_started:
                    logger.info(
                        "[realtime] Noise turn — not opening realtime turn after capture "
                        "(stt=%r, silero_speech=%s, dur=%.2fs); nothing sent to model",
                        combined[:40] if combined else "(empty)",
                        rt_audio_is_speech,
                        buf_duration,
                    )
            else:
                if not voice_cfg.LIVE_MODE:
                    hold_followup()
                start_realtime_turn()
                if (realtime_allowed and realtime_turn_started and turn_context_sent
                        and not realtime_deferred and self._realtime.available
                        and not voice_cfg.LIVE_MODE
                        and early_realtime_result is None and post_capture_wait_filler is None):
                    post_capture_wait_filler = _WaitFiller(owner=interaction_id)
                    if not suppress_auto_fillers and should_arm_realtime_wait_filler(combined):
                        post_capture_wait_filler.arm()
                if not realtime_turn_started and post_capture_wait_filler is not None:
                    post_capture_wait_filler.cancel()
                    post_capture_wait_filler = None

            join_speaker_prepass(
                voice_cfg.SPEAKER_PREPASS_COMMIT_JOIN_S
                if realtime_turn_started and not voice_cfg.LIVE_MODE
                else voice_cfg.SPEAKER_PREPASS_JOIN_S
            )

            if (
                realtime_turn_started
                and realtime_deferred
                and audio_turn is None
                and rt_audio_buffer
                and not is_noise_turn(combined, buf_duration, rt_audio_is_speech)
                and early_realtime_result is None
            ):
                if self._realtime.wait_until_available():
                    try:
                        audio_turn = self._realtime.bind_audio_turn()
                        self._realtime.send_text(turn_context())
                        sent_turn_speaker = turn_speaker_display
                        turn_context_sent = True
                        for audio_f32 in rt_audio_buffer:
                            self._realtime.append_audio(audio_f32, turn=audio_turn)
                        logger.info(
                            "[realtime] Flushed %d buffered frame(s) after noise-drop rebuild",
                            len(rt_audio_buffer),
                        )
                    except AudioTurnSessionChanged:
                        logger.info("[realtime] Deferred upload session changed; retaining full turn for replay")
                    except Exception as e:
                        # Do not commit a partial deferred turn. No audio was
                        # intended for the old activity; falling back preserves
                        # the transcript and avoids contaminating the new session.
                        logger.warning(
                            "[realtime] deferred audio flush failed; falling back: %s", e
                        )
                        rt_audio_buffer.clear()
                else:
                    logger.warning(
                        "[realtime] noise-drop rebuild not ready after capture; "
                        "falling back to main agent"
                    )

            live_opener_consumed = False
            reopen_mic = False
            opener_authorized = harness_listening or should_dispatch_to_main(
                hal_config.WAKEWORD_ENABLED,
                wake_word_confirmed.is_set() or wakeword_followup_active
                or (hal_config.WAKEWORD_ENABLED and self._wakeword_focus.is_active()),
            )
            if (input_policy.automatic and voice_cfg.LIVE_MODE and opener_authorized and combined
                    and not is_noise_turn(combined, buf_duration, rt_audio_is_speech)):
                try:
                    _, opener_type = self._decorator.classify_wake_word(combined)
                except Exception:
                    logger.debug("[live] opener classification unavailable", exc_info=True)
                    opener_type = "voice"
                if (opener_type == "voice" and hal_config.WAKEWORD_ENABLED
                        and not wake_word_confirmed.is_set()):
                    opener_type = "voice_followup"
                # finalize_session trims audio_buffer for speaker identity;
                # SER's snapshot retains the real trailing silence for server VAD.
                live_opener_consumed, reopen_mic = self._try_live_opener(
                    mic, frame_size, device_rate, ser_audio_buffer,
                    transcript=combined, interaction_id=interaction_id,
                    harness_voice=harness_voice, voice_turn_type=opener_type,
                    speaker_display=turn_speaker_display,
                )

            # Wake-word mode only commits a turn authorized by a final wake phrase or
            # the short follow-up focus window.
            if (
                realtime_turn_started
                and turn_context_sent
                and turn_speaker_display
                and turn_speaker_display != sent_turn_speaker
                and early_realtime_result is None
                and hal_config.REALTIME_ENABLED
                and self._realtime.available
                and not is_noise_turn(combined, buf_duration, rt_audio_is_speech)
            ):
                try:
                    self._realtime.send_text(
                        build_speaker_correction(turn_speaker_display)
                    )
                    logger.info(
                        "[realtime] speaker correction sent: context went out with "
                        "%r, voice ID resolved %r",
                        sent_turn_speaker,
                        turn_speaker_display,
                    )
                except Exception as e:
                    logger.warning("[realtime] speaker correction send failed: %s", e)

            if reply_stop is not None and reply_stop.is_set():
                self._wakeword_focus.finish(interaction_id, cancelled=True)
                return False
            if early_realtime_result is not None:
                rt = early_realtime_result
                if rt.handled and (combined or rt.transcript):
                    self._realtime.save_turn(
                        user_text=combined or "(audio only)", agent_text=rt.transcript or "(audio only)",
                    )
            elif strict_rejected:
                rt = RealtimeTurnResult(route=ROUTE_NOT_ADDRESSED)
            elif realtime_turn_started:
                rt = VoiceService._run_automatic_realtime_turn(self, reply_stop,
                    self._realtime,
                    self._tts,
                    self.strip_rt_markers,
                    combined,
                    rt_audio_buffer,
                    buf_duration,
                    rt_audio_is_speech,
                    interaction_id=interaction_id,
                    wait_filler=post_capture_wait_filler,
                    audio_turn=audio_turn,
                    suppress_auto_fillers=suppress_auto_fillers,
                )
            else:
                # No realtime turn was opened this capture. Distinguish the two
                # reasons in the routing log: the noise guard rejected it, or
                # realtime was off / never armed.
                rt = RealtimeTurnResult(
                    route=(
                        ROUTE_NOISE_DROPPED
                        if is_noise_turn(combined, buf_duration, rt_audio_is_speech)
                        else ROUTE_NOT_STARTED
                    )
                )

            if reply_stop is not None and reply_stop.is_set():
                self._wakeword_focus.finish(interaction_id, cancelled=True)
                return False
            wakeword_followup_active = (
                wakeword_followup_active
                or (hal_config.WAKEWORD_ENABLED and self._wakeword_focus.is_active())
            )
            wakeword_authorized = wake_word_confirmed.is_set() or wakeword_followup_active
            dispatch_to_main = input_policy.dispatch_directly or (voice_cfg.LIVE_MODE and opener_authorized) or should_dispatch_to_main(
                hal_config.WAKEWORD_ENABLED,
                wakeword_authorized,
            )
            downstream_dropped = should_drop_downstream_turn(rt)
            if downstream_dropped:
                self._wakeword_focus.finish(interaction_id, cancelled=True)
            if not dispatch_to_main:
                voice_metrics.exclude(interaction_id, voice_metrics.EXCL_NOT_ADDRESSED)
            if (dispatch_to_main and not downstream_dropped and not live_opener_consumed
                    and speaker_prepass_thread is not None):
                join_speaker_prepass()
            if (defer_speaker_prepass and dispatch_to_main and not downstream_dropped
                    and not live_opener_consumed):
                resolve_turn_speaker_identity(after_realtime_decision=True)

            if manual_capture is not None and (
                not self._running
                or endpoint_method != "manual_tap"
                or manual_capture.cancelled.is_set()
                or not same_capture_target(harness_voice, read_voice_mode())
            ):
                dispatch_to_main = False
            if reply_stop is not None and reply_stop.is_set():
                self._wakeword_focus.finish(interaction_id, cancelled=True)
                return False
            if dispatch_to_main and not live_opener_consumed:
                if combined and not downstream_dropped:
                    hold_followup()
                if realtime_allowed and combined and not rt.handled and not downstream_dropped:
                    self._realtime.save_main_handoff(combined)
                # A realtime connection failure or silent timeout is not a
                # handled turn. Preserve the STT fallback so a wake-word command
                # never disappears just because Gemini is temporarily down.
                dispatch_turn(
                    self._decorator,
                    self._sensing_sender,
                    combined,
                    audio_buffer,
                    ser_audio_buffer,
                    rt,
                    interaction_id=interaction_id,
                    event_type_override=input_policy.event_type_override(
                        followup=wakeword_followup_active and not wake_word_confirmed.is_set(),
                    ),
                    identity=turn_identity,
                    harness_voice=harness_voice,
                    suppress_auto_fillers=suppress_auto_fillers,
                )
            elif not live_opener_consumed:
                self._decorator.submit_speech_emotion_from_session(ser_audio_buffer)
                # A rejected utterance deliberately has no downstream agent to replace
                # the listening cue with thinking or TTS. Do not do this for an armed
                # realtime turn: that path may already be expressing an emotion while
                # speaking its direct reply.
                if (
                    hal_config.WAKEWORD_ENABLED
                    and not wake_word_confirmed.is_set()
                    and listening_emotion_sent[0]
                ):
                    from hal import app_state

                    app_state.clear_listening_cue()

            try:
                requests.post(
                    "http://127.0.0.1:5000/api/sensing/event",
                    json={"type": "voice_listening_end", "message": "done"},
                    timeout=0.3,
                )
            except Exception:
                pass

            # Safety net: if we fired emotion=listening but no follow-up emotion arrives
            # (LLM error, silence-only after first partial, TTS interrupt before
            # response), blue-pulse would hang.
            if listening_emotion_sent[0]:
                def _reset_if_still_listening():
                    try:
                        from hal import app_state

                        app_state.clear_listening_cue()
                    except Exception as e:
                        logger.warning("listening idle-reset failed: %s", e)

                threading.Timer(8.0, _reset_if_still_listening).start()

            logger.info("Session RESET — audio_buffer discarded, ready for next turn")
        return reopen_mic
