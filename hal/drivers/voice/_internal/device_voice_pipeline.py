"""Device-only capture producer with bounded, ordered transcript finalization.

The mic thread never waits for STT connect, upload, close, or speaker identity.
A reserved queue slot owns one uploader until it has closed its session; the
single finalizer preserves dispatch order. No runtime/wake/listening cleanup is
performed by a finalizer after a subsequent recording may have started.
"""

from difflib import SequenceMatcher
import logging
import threading
import time

from hal import config as hal_config
from hal.drivers.voice._internal import config as voice_cfg
from hal.drivers.voice._internal.device_capture import DeviceCapture
from hal.drivers.voice._internal.realtime_turn import (
    RealtimeTurnResult, ROUTE_NOISE_DROPPED, ROUTE_NOT_STARTED,
    is_noise_turn, needs_noise_guard,
    should_drop_downstream_turn,
)
from hal.drivers.voice._internal.session_finalize import finalize_session, recent_spoken_text
from hal.drivers.voice._internal.turn_dispatch import dispatch_turn
from hal.telemetry import voice_metrics


logger = logging.getLogger("hal.voice")
_DEFAULT_TTS = object()


def _join_native(chunks):
    if len(chunks) == 1:
        return chunks[0]
    if isinstance(chunks[0], (bytes, bytearray)):
        return b"".join(chunks)
    import numpy as np

    return np.concatenate(chunks, axis=0)


class _RecordedTurn:
    def __init__(self, capture, on_transcript=None):
        self.capture = capture
        self.condition = threading.Condition()
        self.capture_done = threading.Event()
        self.upload_done = threading.Event()
        self.realtime_done = threading.Event()
        self.realtime_result = RealtimeTurnResult()
        self.audio = []
        self.byte_count = 0
        self.partial = [""]
        self.finals = []
        self.session = None
        self.upload_ok = False
        self.upload_started = False
        self.finished_at = None
        self.spoken_text = ""
        self.last_speech_idx = -1
        self.interaction_id = None
        self.on_transcript = on_transcript

    def transcript(self, text, final):
        with self.condition:
            if self.capture.cancelled.is_set() or self.upload_done.is_set():
                return
            if text.strip() and self.on_transcript:
                self.on_transcript(text, final)
            if not final:
                self.partial[0] = text
                return
            previous = self.partial[0]
            # Preserve an earlier segment when a provider's final belongs to a
            # later short phrase, matching the shared STT transcript policy.
            if (previous and len(text) < len(previous)
                    and SequenceMatcher(None, previous.lower(), text.lower()).ratio() < 0.5):
                self.finals.append(previous)
            if text:
                self.finals.append(text)
            self.partial[0] = ""


class DeviceVoicePipeline:
    def __init__(self, queue, *, create_session, convert, valid, set_capturing,
                 tts, decorator, sensing_sender, noise_is_speech,
                 max_duration=None, on_frame=None, on_transcript=None, capture_valid=None,
                 realtime_turn=None, stream_realtime=None, record_handoff=None):
        self.queue = queue
        self.create_session = create_session
        self.convert = convert
        self.valid = valid
        self.capture_valid = capture_valid or valid
        self.set_capturing = set_capturing
        self.tts = tts
        self.decorator = decorator
        self.sensing_sender = sensing_sender
        self.noise_is_speech = noise_is_speech
        self.realtime_turn = realtime_turn
        self.stream_realtime = stream_realtime
        self.record_handoff = record_handoff
        self._previous_realtime_done = threading.Event()
        self._previous_realtime_done.set()
        self.on_frame = on_frame
        self.on_transcript = on_transcript
        self.max_duration = (voice_cfg.MAX_SESSION_DURATION_S
                             if max_duration is None else max_duration)

    def _valid(self, capture):
        return not capture.cancelled.is_set() and self.valid(capture.snapshot)

    def _capture_valid(self, capture):
        return not capture.cancelled.is_set() and self.capture_valid(capture.snapshot)

    def run_capture(self, mic, frame_size, rate, capture, ticket, *, tts=_DEFAULT_TTS):
        """Return after local stop; caller closes mic/releases capture ownership.

        Reserve before accepting the tap, and bind capture.cancelled to
        ticket.cancelled before calling. The caller also owns a TTS guard for
        the complete local recording interval, including ordinary agent replies.
        """
        if capture.cancelled is not ticket.cancelled:
            raise ValueError("capture and reservation must share cancellation")
        turn = _RecordedTurn(capture, self.on_transcript)
        if self.stream_realtime is None:
            turn.realtime_done.set()
        turn_tts = self.tts if tts is _DEFAULT_TTS else tts
        # Activate cleanup ownership before creating any producer resources.
        if not self.queue.submit(
                ticket, lambda cancelled: self._finalize(turn),
                lambda: self._cleanup(turn)):
            self.queue.release(ticket)
            return False
        feedback = DeviceCapture(
            capture, turn_tts, self.set_capturing, lambda: self._capture_valid(capture),
        )
        uploader_started = False
        realtime_started = False
        try:
            if not self._capture_valid(capture):
                capture.cancelled.set()
                return False
            if self.stream_realtime is not None:
                previous = self._previous_realtime_done
                self._previous_realtime_done = turn.realtime_done
                threading.Thread(
                    target=self._stream_realtime, args=(turn, previous),
                    daemon=True, name="device-realtime-stream",
                ).start()
                realtime_started = True
            uploader = threading.Thread(
                target=self._upload, args=(turn,), daemon=True,
                name="device-stt-upload",
            )
            uploader.start()
            uploader_started = True
            turn.upload_started = True
            # This readiness event is local, not the remote STT connection.
            # prepare proves the mic supplies PCM and emits the existing cue.
            local_ready = threading.Event()
            local_ready.set()
            if feedback.prepare(mic, frame_size, rate, local_ready,
                                lambda data: self.convert(data, rate)) is None:
                capture.cancelled.set()
                return False
            started = time.monotonic()
            checked_at = started
            max_bytes = int(self.max_duration * voice_cfg.STT_RATE * 2)
            read_size = min(frame_size, max(1, rate // 100))
            pending = []
            pending_frames = 0
            captured_frames = 0
            uploaded_frames = 0
            speech_until = 0

            def append_frame(data, samples):
                nonlocal uploaded_frames
                pcm = self.convert(data, rate)
                if turn.byte_count + len(pcm) > max_bytes:
                    capture.cancelled.set()
                    return False
                with turn.condition:
                    turn.audio.append(pcm)
                    turn.byte_count += len(pcm)
                    if speech_until > uploaded_frames:
                        turn.last_speech_idx = len(turn.audio) - 1
                    uploaded_frames += samples
                    turn.condition.notify_all()
                return True

            while not capture.cancelled.is_set():
                if capture.finished.is_set():
                    if pending and not append_frame(_join_native(pending), pending_frames):
                        return False
                    turn.finished_at = time.monotonic()
                    turn.interaction_id = voice_metrics.speech_end("manual_tap", at=turn.finished_at)
                    turn.spoken_text = recent_spoken_text(turn_tts)
                    # Publish the endpoint before the synchronous finish tone.
                    # Realtime commits in parallel with local feedback/STT drain.
                    turn.capture_done.set()
                    with turn.condition:
                        turn.condition.notify_all()
                    feedback.stop()
                    return True
                now = time.monotonic()
                if now - checked_at >= 0.25:
                    checked_at = now
                    if not self._capture_valid(capture):
                        capture.cancelled.set()
                        break
                if now - started >= self.max_duration:
                    logger.warning("Device recording exceeded capture limit; discarding")
                    capture.cancelled.set()
                    break
                # Poll physical stop at 10 ms, while retaining the shared 64 ms
                # PCM frame contract for uploads, speaker trimming and SER.
                data, overflowed = mic.read(read_size)
                if overflowed:
                    continue
                sample_width = 2 if isinstance(data, (bytes, bytearray)) else 1
                samples = len(data) // sample_width
                captured_frames += samples
                if captured_frames > int(self.max_duration * rate):
                    capture.cancelled.set()
                    break
                pending.append(data)
                pending_frames += samples
                if self.on_frame is None or self.on_frame(data):
                    speech_until = captured_frames
                while pending_frames >= frame_size:
                    native = _join_native(pending)
                    split = frame_size * sample_width
                    if not append_frame(native[:split], frame_size):
                        break
                    pending_frames -= frame_size
                    pending = [native[split:]] if pending_frames else []
            return False
        except Exception:
            capture.cancelled.set()
            logger.exception("Device recording failed")
            return False
        finally:
            try:
                if not feedback.stopped:
                    self.set_capturing(False)
            finally:
                turn.capture_done.set()
                with turn.condition:
                    turn.condition.notify_all()
                if not uploader_started:
                    # Session construction/thread startup may fail before the
                    # uploader takes ownership. Cleanup still runs on the worker.
                    turn.upload_done.set()
                if not realtime_started:
                    turn.realtime_done.set()

    def _stream_realtime(self, turn, previous):
        """Serialize provider ownership independently of STT/FIFO dispatch.

        At most two reservations exist, so at most two streaming workers and
        their bounded PCM buffers can exist. A newer turn can upload as soon as
        the previous model reply finishes, even while its STT still drains.
        """
        capture = turn.capture

        def frames():
            sent = 0
            while not capture.cancelled.is_set():
                with turn.condition:
                    turn.condition.wait_for(
                        lambda: len(turn.audio) > sent or turn.capture_done.is_set()
                        or capture.cancelled.is_set(), timeout=0.05,
                    )
                    batch = turn.audio[sent:]
                    finished = turn.capture_done.is_set()
                for frame in batch:
                    if capture.cancelled.is_set():
                        return
                    yield frame
                    sent += 1
                if finished:
                    return

        def snapshot():
            with turn.condition:
                combined = " ".join([*turn.finals, turn.partial[0]]).strip()
            return dict(combined=combined, duration=turn.byte_count / (voice_cfg.STT_RATE * 2),
                        speech=turn.last_speech_idx >= 0, interaction_id=turn.interaction_id,
                        speaker=None, finished_at=turn.finished_at)

        try:
            while not previous.wait(0.05):
                if capture.cancelled.is_set():
                    return
            if self._valid(capture):
                turn.realtime_result = self.stream_realtime(
                    frames(), snapshot=snapshot, cancelled=capture.cancelled,
                    valid=lambda: self._valid(capture),
                )
        except Exception:
            logger.exception("Device realtime streaming failed; retaining STT fallback")
        finally:
            turn.realtime_done.set()

    def _upload(self, turn):
        """One session owner; late connection is always closed on cancellation."""
        capture = turn.capture
        try:
            turn.session = self.create_session()
            if capture.cancelled.is_set() or not turn.session.start(turn.transcript):
                if self.stream_realtime is None:
                    capture.cancelled.set()
                return
            sent = 0
            while not capture.cancelled.is_set():
                with turn.condition:
                    turn.condition.wait_for(
                        lambda: len(turn.audio) > sent or turn.capture_done.is_set()
                        or capture.cancelled.is_set(), timeout=0.1,
                    )
                    batch = turn.audio[sent:]
                    finished = turn.capture_done.is_set()
                for frame in batch:
                    if capture.cancelled.is_set():
                        return
                    if turn.session.is_closed():
                        raise RuntimeError("STT session closed before capture upload completed")
                    turn.session.send_audio(frame)
                    sent += 1
                if finished:
                    turn.upload_ok = not capture.cancelled.is_set()
                    return
        except Exception:
            if self.stream_realtime is None:
                capture.cancelled.set()
            logger.exception("Device STT upload failed")
        finally:
            try:
                if turn.session is not None:
                    turn.session.close()
            except Exception:
                if self.stream_realtime is None:
                    capture.cancelled.set()
                logger.exception("Device STT close failed")
            finally:
                turn.upload_done.set()

    def _cleanup(self, turn):
        # Never free capacity while its uploader/session still owns resources.
        # Provider start/send/close must retain their network timeout contracts.
        turn.capture_done.wait()
        turn.upload_done.wait()
        turn.realtime_done.wait()
        if turn.session is not None and not turn.upload_started:
            turn.session.close()
        turn.audio.clear()

    def _finalize(self, turn):
        turn.capture_done.wait()
        turn.realtime_done.wait()
        turn.upload_done.wait()
        capture = turn.capture
        if ((not turn.upload_ok and self.stream_realtime is None)
                or turn.finished_at is None or not self._valid(capture)):
            return
        combined, ser_audio, duration = finalize_session(
            turn.audio, turn.partial, turn.finals, turn.last_speech_idx,
            turn.spoken_text,
        )
        interaction_id = turn.interaction_id
        speech = True
        if (needs_noise_guard(combined) and hal_config.REALTIME_REQUIRE_SPEECH_ON_EMPTY_STT
                and turn.audio):
            try:
                speech = self.noise_is_speech(b"".join(turn.audio))
            except Exception:
                logger.exception("Device speech guard failed")
        noise = is_noise_turn(combined, duration, speech)
        identity = None
        if combined and not noise:
            text, _ = self.decorator.classify_wake_word(combined)
            try:
                identity = self.decorator.identify_and_decorate(text, turn.audio)
            except Exception:
                logger.exception("Device speaker identification failed; retaining transcript")
                identity = (text, None, None)
        # Identity can perform remote inference; recheck route/privacy after it.
        if not self._valid(capture):
            return
        result = RealtimeTurnResult(route=ROUTE_NOISE_DROPPED if noise else ROUTE_NOT_STARTED)
        if self.stream_realtime is not None:
            result = turn.realtime_result
            if (noise and not result.handled and not (result.delegated and result.delegate_msg)
                    and not should_drop_downstream_turn(result)):
                result = RealtimeTurnResult(route=ROUTE_NOISE_DROPPED)
        elif not noise and self.realtime_turn is not None:
            result = self.realtime_turn(
                audio=turn.audio, combined=combined, duration=duration, speech=speech,
                interaction_id=interaction_id, speaker=identity[2] if identity else None,
                cancelled=capture.cancelled, valid=lambda: self._valid(capture),
            )
        # Realtime can wait on a provider or on playback admission while a newer
        # recording owns the mic. Never hand off a cancelled or rerouted turn.
        if not self._valid(capture):
            return
        if (self.stream_realtime is not None and self.record_handoff is not None
                and combined and not result.handled and not should_drop_downstream_turn(result)):
            self.record_handoff(combined)
        logger.info("Device session END — bytes=%d transcript=%r interaction_id=%s",
                    turn.byte_count, combined or "(empty)", interaction_id)
        dispatch_turn(
            self.decorator, self.sensing_sender, combined, turn.audio, ser_audio,
            result,
            event_type_override="voice_command", identity=identity,
            interaction_id=interaction_id, harness_voice=capture.snapshot,
        )
