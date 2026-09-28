"""VAD filter wrappers — WebRTC + Silero.

Each filter encapsulates its own state and exposes `is_speech(data, device_rate)`
returning True/False. Fail-open: if the underlying model isn't available, returns
True so callers don't drop legitimate speech.
"""

import logging
import threading
from math import gcd
from pathlib import Path
from typing import Optional

from hal.drivers.voice._internal.config import (
    STT_RATE,
    SILERO_CHUNK_SIZE,
    SILERO_VAD_THRESHOLD,
    WEBRTCVAD_FRAME_MS,
)

logger = logging.getLogger("hal.voice")


# --- Shared Silero ONNX session ------------------------------------------
# One session per model path for the whole process. Four callers used to build
# their own from the same file — three SileroVADFilter slots in voice_service
# (entry gate, realtime noise guard, silence clock) plus the SER prefilter —
# and each session costs a model load and tens of MB of arenas.
#
# Sharing is safe because `InferenceSession.run()` is thread-safe and Silero's
# LSTM `state` / 64-sample `context` travel in and out as per-call TENSORS
# rather than living in the session. Every caller already keeps its own pair,
# so concurrent callers cannot corrupt each other. Verified against this exact
# model: 4 threads x 60 chunks produced output bit-identical to running them
# sequentially.
#
# Deliberately NOT guarded by a lock: serializing the session would put the
# live capture thread behind the SER worker's sweep, which is the one thing
# this must not do.
_shared_silero: dict[str, Optional[object]] = {}
_shared_silero_lock = threading.Lock()


def _build_silero_session(model_path: Path) -> Optional[object]:
    """Construct one Silero session, or None if it cannot be loaded."""
    if not model_path.exists():
        logger.info("Silero VAD model not found at %s — disabled", model_path)
        return None
    try:
        import os as _os
        _os.environ.setdefault("OMP_NUM_THREADS", "1")
        _os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        session = ort.InferenceSession(
            str(model_path),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        logger.info("Silero VAD session loaded (shared): %s", model_path)
        return session
    except Exception as e:
        logger.warning("Silero VAD not available — falling back to RMS only: %s", e)
        return None


def shared_silero_session(model_path: Path) -> Optional[object]:
    """Return the process-wide Silero session for ``model_path``.

    Returns None when the model is missing or onnxruntime cannot load it; every
    caller then falls back to its own RMS-only path, exactly as before. The
    None is cached alongside a real session on purpose — the failure belongs to
    the file and the runtime, not to the caller, so retrying per caller would
    only re-log the same error two or three more times at boot.
    """
    key = str(model_path)
    with _shared_silero_lock:
        if key not in _shared_silero:
            _shared_silero[key] = _build_silero_session(model_path)
        return _shared_silero[key]


class WebRTCVADFilter:
    """Fast C-based VAD (~0.1ms/frame). One instance per aggressiveness level.

    Aggressiveness 0-3 (3 = most strict); the STT entry gate runs at
    HAL_WEBRTCVAD_AGGRESSIVENESS (default 2).
    """

    def __init__(self, aggressiveness: int, np):
        self._np = np
        self._vad = None
        try:
            import webrtcvad as _webrtcvad
            self._vad = _webrtcvad.Vad(aggressiveness)
            logger.info("WebRTC VAD loaded (aggressiveness=%d)", aggressiveness)
        except ImportError as e:
            # Name the module that actually failed. webrtcvad imports
            # pkg_resources at its own line 1, and Python 3.12 stopped seeding
            # setuptools into new venvs — so an installed webrtcvad still
            # raises ModuleNotFoundError, a subclass of ImportError. Reporting
            # that as "webrtcvad not installed" sends whoever reads the log to
            # reinstall a package that was already there (measured on
            # lamp-0c89, where the fix was setuptools). This gate fails OPEN,
            # so the only sign anything is wrong is this one line.
            logger.warning(
                "WebRTC VAD unavailable, entry gate disabled (passes everything): %s", e
            )
        except Exception as e:
            logger.warning("WebRTC VAD not available: %s", e)

    @property
    def available(self) -> bool:
        return self._vad is not None

    def is_speech(self, data, device_rate: int) -> bool:
        """Returns True if any 30ms chunk of `data` contains speech.

        Fails open (returns True) if VAD unavailable or errors — don't drop
        legitimate speech on infrastructure issues.
        """
        if self._vad is None:
            return True
        try:
            np = self._np
            if device_rate != STT_RATE:
                import scipy.signal
                samples = data.flatten().astype(np.float32)
                g = gcd(STT_RATE, device_rate)
                audio_16k = scipy.signal.resample_poly(samples, STT_RATE // g, device_rate // g).astype(np.int16)
            else:
                audio_16k = data.flatten().astype(np.int16)
            frame_samples = int(STT_RATE * WEBRTCVAD_FRAME_MS / 1000)
            raw = audio_16k.tobytes()
            frame_bytes = frame_samples * 2
            for i in range(0, len(raw) - frame_bytes + 1, frame_bytes):
                if self._vad.is_speech(raw[i:i + frame_bytes], STT_RATE):
                    return True
            return False
        except Exception as e:
            logger.warning("WebRTC VAD error: %s", e)
            return True


class SileroVADFilter:
    """Semantic VAD (ONNX) — rejects TV, music, and other non-speech audio that
    fools energy-based VAD. Slower (~20ms/frame on ARM) so runs AFTER WebRTC.

    Stateful: maintains LSTM hidden state across calls (reset between sessions
    via `reset_state()`). Silero v5+ requires a 64-sample context prepended to
    each chunk — handled internally.
    """

    def __init__(self, model_path: Path, np):
        self._np = np
        self._state = None
        self._context = None
        # Guards THIS instance's `_state` / `_context` only.
        #
        # It used to guard the session too, as a side effect of each instance
        # owning one. The session is now shared process-wide, so this lock no
        # longer serializes it — which is correct: run() is thread-safe and all
        # model state is in the per-call tensors this lock protects. Do not
        # widen it to cover the session; that would block the capture thread
        # behind the SER worker.
        self._lock = threading.Lock()
        self._session = shared_silero_session(model_path)
        if self._session is not None:
            self.reset_state()
            logger.info("Silero VAD ready (threshold=%.2f)", SILERO_VAD_THRESHOLD)

    @property
    def available(self) -> bool:
        return self._session is not None

    def reset_state(self) -> None:
        """Reset LSTM hidden state + context between speech segments."""
        np = self._np
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        # Silero v5+ requires 64 context samples (16kHz) prepended to each chunk
        self._context = np.zeros((1, 64), dtype=np.float32)

    def is_speech(self, data, device_rate: int) -> bool:
        """Run Silero on `data`. Returns True if peak confidence ≥ threshold.

        Fails open (returns True) on infrastructure errors — don't drop speech.
        """

        if self._session is None:
            return True
        try:
            np = self._np
            if device_rate != STT_RATE:
                import scipy.signal
                samples = data.flatten().astype(np.float32)
                g = gcd(STT_RATE, device_rate)
                up, down = STT_RATE // g, device_rate // g
                audio_16k = scipy.signal.resample_poly(samples, up, down).astype(np.float32)
            else:
                audio_16k = data.flatten().astype(np.float32)

            # Normalize int16 → float32 [-1, 1]
            audio_norm = audio_16k / 32768.0

            max_conf = 0.0
            with self._lock:
                for i in range(0, len(audio_norm), SILERO_CHUNK_SIZE):
                    chunk = audio_norm[i:i + SILERO_CHUNK_SIZE]
                    if len(chunk) < SILERO_CHUNK_SIZE:
                        chunk = np.pad(chunk, (0, SILERO_CHUNK_SIZE - len(chunk)))
                    # Silero v5+: prepend 64-sample context from previous chunk
                    x = np.concatenate([self._context, chunk.reshape(1, -1)], axis=1)
                    out = self._session.run(
                        None,
                        {
                            "input": x,
                            "state": self._state,
                            "sr": np.array(STT_RATE, dtype=np.int64),
                        },
                    )
                    max_conf = max(max_conf, float(out[0][0][0]))
                    self._state = out[1]
                    self._context = x[:, -64:]

            is_speech = max_conf >= SILERO_VAD_THRESHOLD
            if not is_speech:
                logger.info("Silero: conf=%.3f < threshold=%.2f — rejected", max_conf, SILERO_VAD_THRESHOLD)
            return is_speech
        except Exception as e:
            logger.warning("Silero VAD inference error: %s", e)
            return True

    def speech_metrics(self, data, device_rate: int):
        """Return (peak, mean, voiced_ratio, span_ratio, span_seconds) for `data`.

        Unlike is_speech (which is peak-only — one transient chunk crossing the
        threshold marks the whole buffer speech, fine for fast onset detection at
        the entry gate but too lenient to REJECT a noisy turn), this reports the
        fraction of 32ms chunks that are voiced. A real speaking turn is voiced
        across most of its length; sustained noise has only sparse voiced chunks.

        `voiced_ratio` is that fraction over the WHOLE buffer; `span_ratio` is
        the same fraction measured only between the first and last voiced chunk.
        They differ when a buffer carries silence around the speech — a captured
        turn always does, since the session prepends VAD pre-roll and keeps a
        200ms tail — and the padding then drags voiced_ratio down in proportion
        to how SHORT the utterance is. Judging a whole turn wants span_ratio;
        judging a live sliding window wants voiced_ratio, since there is no
        padding to discount and a span measure would only be more lenient.

        `span_seconds` is the WALL LENGTH of that same span — the actual
        utterance, with the padding excluded. It is not the buffer duration the
        caller already has: a capture is padded at both ends, so a single word
        still yields a multi-second buffer (measured on lamp-0c89: no buffer
        shorter than 1.73s in 14 days, `"the"` alone arriving as 3.46s). Any
        gate meaning "too short to be addressed to us" has to read this, not the
        buffer.

        Fails open: returns (1.0, 1.0, 1.0, 1.0, 0.0) on error so callers treat
        it as speech. The 0.0 span is deliberately NOT a plausible utterance
        length — a fail-open path must not hand a duration gate a number it can
        act on.
        """
        if self._session is None:
            return (1.0, 1.0, 1.0, 1.0, 0.0)
        try:
            np = self._np
            if device_rate != STT_RATE:
                import scipy.signal
                samples = data.flatten().astype(np.float32)
                g = gcd(STT_RATE, device_rate)
                up, down = STT_RATE // g, device_rate // g
                audio_16k = scipy.signal.resample_poly(samples, up, down).astype(np.float32)
            else:
                audio_16k = data.flatten().astype(np.float32)
            audio_norm = audio_16k / 32768.0

            confs = []
            with self._lock:
                for i in range(0, len(audio_norm), SILERO_CHUNK_SIZE):
                    chunk = audio_norm[i:i + SILERO_CHUNK_SIZE]
                    if len(chunk) < SILERO_CHUNK_SIZE:
                        chunk = np.pad(chunk, (0, SILERO_CHUNK_SIZE - len(chunk)))
                    x = np.concatenate([self._context, chunk.reshape(1, -1)], axis=1)
                    out = self._session.run(
                        None,
                        {
                            "input": x,
                            "state": self._state,
                            "sr": np.array(STT_RATE, dtype=np.int64),
                        },
                    )
                    confs.append(float(out[0][0][0]))
                    self._state = out[1]
                    self._context = x[:, -64:]

            if not confs:
                return (1.0, 1.0, 1.0, 1.0, 0.0)
            peak = max(confs)
            mean = sum(confs) / len(confs)
            voiced = [c >= SILERO_VAD_THRESHOLD for c in confs]
            ratio = sum(voiced) / len(voiced)
            # Span: first..last voiced chunk inclusive. No voiced chunk at all
            # means there is no span to measure and span_ratio collapses to the
            # whole-buffer ratio (0.0) — the reject path, which is correct.
            if any(voiced):
                first = voiced.index(True)
                last = len(voiced) - 1 - voiced[::-1].index(True)
                span = voiced[first:last + 1]
                span_ratio = sum(span) / len(span)
                span_seconds = len(span) * SILERO_CHUNK_SIZE / STT_RATE
            else:
                span_ratio = ratio
                span_seconds = 0.0
            return (peak, mean, ratio, span_ratio, span_seconds)
        except Exception as e:
            logger.warning("Silero speech_metrics error: %s", e)
            return (1.0, 1.0, 1.0, 1.0, 0.0)


def turn_should_close(
    now: float, last_speech_time: float, final_ts: float
) -> bool:
    """Whether the capture loop should end the turn on this silent frame.

    Two clocks. The fallback is the long one: silence since the last confirmed
    speech beyond SILENCE_TIMEOUT_S. The short one only applies once STT has
    emitted a final segment, because the provider has then made its own
    end-of-turn call, and sitting on the long clock afterwards is dead air in
    front of the realtime commit.

    The short clock is measured from the FINAL'S ARRIVAL, not from the last
    speech, and that is the whole subtlety. Flux emits an EndOfTurn for natural
    pauses INSIDE one utterance ("Hello." while the speaker draws breath before
    "Can you hear me?"). Measuring from the last speech applies the short budget
    retroactively to silence that had already accumulated, so such a final
    closes the session on the very next frame — device-observed 04/09/2026,
    lamp-0c89: final 'Hello.' at 09:22:50.766, session closed at 09:22:50.880,
    114ms later, while the user was still mid-sentence. Measuring from the
    final instead gives the speaker a real ENDPOINT_SILENCE_S window to carry
    on, and still closes that much after a final that genuinely ended the turn.

    Confirmed speech after that final supersedes its endpoint. Until a new
    final arrives, a pause in the resumed speech uses the long fallback clock.
    """
    from hal.drivers.voice._internal.config import (
        ENDPOINT_SILENCE_S,
        SILENCE_TIMEOUT_S,
    )

    silence: float = now - last_speech_time
    if final_ts > 0 and final_ts >= last_speech_time and ENDPOINT_SILENCE_S > 0:
        if now - final_ts >= ENDPOINT_SILENCE_S and silence >= ENDPOINT_SILENCE_S:
            return True
    return silence > SILENCE_TIMEOUT_S
