"""VAD filter wrappers — WebRTC + Silero."""

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


# One session per model path for the whole process.
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
    """Return the process-wide Silero session for ``model_path``."""
    key = str(model_path)
    with _shared_silero_lock:
        if key not in _shared_silero:
            _shared_silero[key] = _build_silero_session(model_path)
        return _shared_silero[key]


class WebRTCVADFilter:
    """Fast C-based VAD (~0.1ms/frame). One instance per aggressiveness level."""

    def __init__(self, aggressiveness: int, np):
        self._np = np
        self._vad = None
        try:
            import webrtcvad as _webrtcvad
            self._vad = _webrtcvad.Vad(aggressiveness)
            logger.info("WebRTC VAD loaded (aggressiveness=%d)", aggressiveness)
        except ImportError as e:
            # Name the module that actually failed.
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

        Fails open (returns True) if VAD unavailable or errors — don't drop legitimate
        speech on infrastructure issues.
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
    """Semantic VAD (ONNX) — rejects TV, music, and other non-speech audio that fools
    energy-based VAD.
    """

    def __init__(self, model_path: Path, np):
        self._np = np
        self._state = None
        self._context = None
        # Guards THIS instance's `_state` / `_context` only.
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

            audio_norm = audio_16k / 32768.0

            max_conf = 0.0
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

        The 0.0 span is deliberately NOT a plausible utterance length — a fail-open path
        must not hand a duration gate a number it can act on.
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
    """Whether the capture loop should end the turn on this silent frame."""
    from hal.drivers.voice._internal.config import (
        ENDPOINT_SILENCE_S,
        SILENCE_TIMEOUT_S,
    )

    silence: float = now - last_speech_time
    if final_ts > 0 and final_ts >= last_speech_time and ENDPOINT_SILENCE_S > 0:
        if now - final_ts >= ENDPOINT_SILENCE_S and silence >= ENDPOINT_SILENCE_S:
            return True
    return silence > SILENCE_TIMEOUT_S
