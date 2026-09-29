"""emotion2vec recognizer — POSTs WAV to perception-service /api/dl/ser/recognize."""

from __future__ import annotations

import base64
import json
import logging
from typing import Optional

import numpy as np
import numpy.typing as npt
import requests

import hal.config as config
from hal.drivers.sensing.crypto import CryptoSession, resolve_public_key
from hal.drivers.voice.speech_emotion.base import (
    BaseSpeechEmotionRecognizer,
    SpeechEmotionResult,
)
from hal.drivers.voice._internal.config import (
    SILERO_MODEL_PATH as _SILERO_MODEL_PATH,
)
from hal.drivers.voice._internal.vad_filters import shared_silero_session
from hal.drivers.voice.speech_emotion.constants import (
    DEFAULT_API_TIMEOUT_S,
    PREFILTER_FRAME_MS,
    PREFILTER_MIN_TRIMMED_S,
    PREFILTER_MIN_VOICED_RATIO,
    PREFILTER_MIN_VOICED_S,
    PREFILTER_PAD_MS,
    PREFILTER_SAMPLE_RATE,
    PREFILTER_SILERO_CHUNK_SAMPLES,
    PREFILTER_SILERO_CONTEXT_SAMPLES,
    PREFILTER_SILERO_THRESHOLD,
    PREFILTER_TRIM_RMS,
    PREFILTER_VAD_FALLBACK_MIN_VOICED_S,
    PREFILTER_VAD_MIN_VOICED_S,
    PREFILTER_VOICED_RMS,
    SER_MAX_CLIP_S,
)
from hal.drivers.voice.speech_emotion.debug_tracer import (
    audio_stats,
    tracer,
)
from hal.drivers.voice.speech_emotion.utils import (
    compute_trim_and_voiced,
    pcm16_to_wav,
    select_voiced_span,
    wav_to_pcm16,
)

logger = logging.getLogger("hal.voice.speech_emotion.engine")

# Silero VAD ONNX model.


class Emotion2VecRecognizer(BaseSpeechEmotionRecognizer):
    """HTTP wrapper around perception-service `/api/dl/ser/recognize`."""

    def __init__(
        self,
        url: str,
        api_key: str = "",
        timeout_s: float = DEFAULT_API_TIMEOUT_S,
    ):
        self._url: str = url or ""
        self._api_key: str = api_key or ""
        self._timeout: float = timeout_s

        self._crypto: CryptoSession | None = None
        if config.DL_ENCRYPTION_ENABLED:
            public_key = resolve_public_key(config.DL_PUBLIC_KEY_URL, config.DL_API_KEY, config.DL_PUBLIC_KEY_FILE)
            if public_key is not None:
                self._crypto = CryptoSession(public_key)
                logger.info("[speech_emotion.engine] encryption enabled")
            elif config.DL_ENCRYPTION_REQUIRED:
                raise RuntimeError("Encryption required but no public key available")

        self._silero = self._load_silero()

    @property
    def available(self) -> bool:
        return bool(self._url)

    def recognize(self, wav_bytes: bytes) -> Optional[SpeechEmotionResult]:
        tracer.note_section("engine", {
            "url": self._url,
            "timeout_s": self._timeout,
            "api_key_set": bool(self._api_key),
            "encrypted": self._crypto is not None,
            "silero_loaded": self._silero is not None,
        })
        if not self._url:
            logger.warning("[speech_emotion.engine] recognize skipped — empty URL")
            tracer.fail("engine-no-url")
            return None
        if not wav_bytes:
            logger.warning("[speech_emotion.engine] recognize skipped — empty wav")
            tracer.fail("engine-empty-wav")
            return None
        with tracer.stage("prefilter"):
            filtered = self.prefilter(wav_bytes)
        if filtered is None:
            return None
        wav_bytes = filtered
        with tracer.stage("encode_b64"):
            b64 = base64.b64encode(wav_bytes).decode("ascii")
        logger.debug(
            "[speech_emotion.engine] POST %s (wav=%d bytes, b64=%d chars, timeout=%.1fs)",
            self._url, len(wav_bytes), len(b64), self._timeout,
        )
        tracer.note_section("http", {
            "request_wav_bytes": len(wav_bytes),
            "request_b64_chars": len(b64),
        })
        try:
            payload = {"audio_b64": b64, "return_scores": False}
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if self._api_key:
                headers["X-API-Key"] = self._api_key
            with tracer.stage("api_request"):
                if self._crypto is not None:
                    resp = requests.post(
                        self._url,
                        data=self._crypto.wrap_http_request(json.dumps(payload).encode()),
                        headers=headers, timeout=self._timeout,
                    )
                else:
                    resp = requests.post(
                        self._url, json=payload, headers=headers, timeout=self._timeout,
                    )
        except requests.RequestException as e:
            logger.warning("[speech_emotion.engine] request failed: %s", e)
            tracer.fail(
                "request-failed",
                http_error={"type": type(e).__name__, "message": str(e)},
            )
            return None

        tracer.note_section("http", {
            "status_code": resp.status_code,
            "response_bytes": len(resp.content or b""),
        })
        if resp.status_code != 200:
            logger.warning(
                "[speech_emotion.engine] HTTP %d: %s",
                resp.status_code, resp.text[:200],
            )
            tracer.fail(
                f"http-{resp.status_code}",
                http_error={"body": resp.text[:500]},
            )
            return None

        try:
            with tracer.stage("api_decode"):
                if self._crypto is not None:
                    data = json.loads(self._crypto.unwrap_http_response(resp.content))
                else:
                    data = resp.json()
        except ValueError:
            logger.warning(
                "[speech_emotion.engine] non-JSON response: %s", resp.text[:200],
            )
            tracer.fail(
                "non-json-response", http_error={"body": resp.text[:500]},
            )
            return None

        tracer.note_section("http", {"response": data})
        label = data.get("label")
        if not label:
            logger.warning(
                "[speech_emotion.engine] response missing 'label': %s",
                str(data)[:200],
            )
            tracer.fail("missing-label")
            return None
        confidence = float(data.get("confidence", 0.0))
        logger.debug(
            "[speech_emotion.engine] response OK: label=%s confidence=%.3f",
            label, confidence,
        )
        return SpeechEmotionResult(label=label, confidence=confidence)

    def prefilter(self, wav_bytes: bytes) -> Optional[bytes]:
        """Apply the RMS + Silero prefilter to ``wav_bytes``."""
        tracer.note_section("prefilter", {
            "thresholds": {
                "sample_rate": PREFILTER_SAMPLE_RATE,
                "frame_ms": PREFILTER_FRAME_MS,
                "pad_ms": PREFILTER_PAD_MS,
                "trim_rms": PREFILTER_TRIM_RMS,
                "voiced_rms": PREFILTER_VOICED_RMS,
                "min_trimmed_s": PREFILTER_MIN_TRIMMED_S,
                "min_voiced_s": PREFILTER_MIN_VOICED_S,
                "min_voiced_ratio": PREFILTER_MIN_VOICED_RATIO,
                "silero_threshold": PREFILTER_SILERO_THRESHOLD,
                "silero_min_voiced_s": PREFILTER_VAD_MIN_VOICED_S,
                "silero_off_fallback_min_voiced_s": PREFILTER_VAD_FALLBACK_MIN_VOICED_S,
            },
        })
        try:
            with tracer.stage("decode_wav"):
                samples, sample_rate = wav_to_pcm16(wav_bytes)
        except Exception as e:
            logger.warning("[prefilter] DROP — wav decode failed: %s", e)
            tracer.fail("prefilter-decode-failed", decode_error=str(e))
            return None
        if samples.size == 0 or sample_rate <= 0:
            logger.info("[prefilter] DROP — empty audio after decode")
            tracer.fail("prefilter-empty-audio")
            return None
        if sample_rate != PREFILTER_SAMPLE_RATE:
            logger.info(
                "[prefilter] DROP — unexpected sample_rate=%d (expected %d)",
                sample_rate, PREFILTER_SAMPLE_RATE,
            )
            tracer.fail(
                "prefilter-bad-sample-rate", input_sample_rate=int(sample_rate),
            )
            return None
        total_s = samples.size / sample_rate

        with tracer.stage("rms_trim"):
            trimmed, voiced_s, ratio = compute_trim_and_voiced(
                samples, sample_rate,
                PREFILTER_TRIM_RMS, PREFILTER_VOICED_RMS,
                PREFILTER_FRAME_MS, PREFILTER_PAD_MS,
            )
        trimmed_s = trimmed.size / sample_rate
        tracer.note_section("prefilter", {
            "total_s": round(total_s, 3),
            "trimmed_s": round(trimmed_s, 3),
            "rms_voiced_s": round(voiced_s, 3),
            "rms_voiced_ratio": round(ratio, 4),
        })
        if trimmed_s < PREFILTER_MIN_TRIMMED_S:
            logger.info(
                "[prefilter] DROP — trim too short: total=%.2fs trim=%.2fs < %.2fs",
                total_s, trimmed_s, PREFILTER_MIN_TRIMMED_S,
            )
            tracer.fail("prefilter-trim-too-short")
            return None

        if voiced_s < PREFILTER_MIN_VOICED_S:
            logger.info(
                "[prefilter] DROP — RMS voiced=%.2fs < %.2fs "
                "(total=%.2fs trim=%.2fs ratio=%.0f%%)",
                voiced_s, PREFILTER_MIN_VOICED_S,
                total_s, trimmed_s, ratio * 100,
            )
            tracer.fail("prefilter-rms-voiced-too-short")
            return None
        if ratio < PREFILTER_MIN_VOICED_RATIO:
            logger.info(
                "[prefilter] DROP — RMS voiced_ratio=%.0f%% < %.0f%% "
                "(voiced=%.2fs trim=%.2fs)",
                ratio * 100, PREFILTER_MIN_VOICED_RATIO * 100,
                voiced_s, trimmed_s,
            )
            tracer.fail("prefilter-low-voiced-ratio")
            return None

        # Stage 2 — Silero VAD on the trimmed buffer
        with tracer.stage("silero_vad"):
            silero_voiced_s = self._silero_voiced_seconds(trimmed, sample_rate)
        tracer.note_section("prefilter", {
            "silero_voiced_s": (
                None if silero_voiced_s is None else round(silero_voiced_s, 3)
            ),
        })
        if silero_voiced_s is None:
            # Silero unavailable — fall back to a stricter RMS bar so we
            # don't ship "may-be-noise" audio to the model unchecked.
            if voiced_s < PREFILTER_VAD_FALLBACK_MIN_VOICED_S:
                logger.info(
                    "[prefilter] DROP — Silero off and RMS voiced=%.2fs < fallback %.2fs",
                    voiced_s, PREFILTER_VAD_FALLBACK_MIN_VOICED_S,
                )
                tracer.fail("prefilter-silero-off-rms-too-short")
                return None
            logger.info(
                "[prefilter] PASS (Silero off, RMS fallback) "
                "total=%.2fs trim=%.2fs rms_voiced=%.2fs (%.0f%%)",
                total_s, trimmed_s, voiced_s, ratio * 100,
            )
            tracer.note_section(
                "prefilter", {"verdict": "pass-silero-off-rms-fallback"},
            )
        elif silero_voiced_s < PREFILTER_VAD_MIN_VOICED_S:
            logger.info(
                "[prefilter] DROP — Silero voiced=%.2fs < %.2fs "
                "(total=%.2fs trim=%.2fs rms_voiced=%.2fs ratio=%.0f%%)",
                silero_voiced_s, PREFILTER_VAD_MIN_VOICED_S,
                total_s, trimmed_s, voiced_s, ratio * 100,
            )
            tracer.fail("prefilter-silero-voiced-too-short")
            return None
        else:
            logger.info(
                "[prefilter] PASS total=%.2fs trim=%.2fs "
                "rms_voiced=%.2fs (%.0f%%) silero=%.2fs",
                total_s, trimmed_s, voiced_s, ratio * 100, silero_voiced_s,
            )
            tracer.note_section("prefilter", {"verdict": "pass"})

        clipped = select_voiced_span(
            trimmed, sample_rate, PREFILTER_VOICED_RMS, PREFILTER_FRAME_MS, SER_MAX_CLIP_S,
        )
        if clipped.size != trimmed.size:
            logger.info(
                "[prefilter] CLIP %.2fs → %.2fs (most-voiced span)",
                trimmed.size / sample_rate, clipped.size / sample_rate,
            )
        tracer.note_section("prefilter", {
            "clipped_s": round(clipped.size / sample_rate, 3),
        })
        trimmed = clipped

        try:
            with tracer.stage("encode_wav"):
                out_wav = pcm16_to_wav(trimmed, sample_rate)
        except Exception as e:
            logger.warning(
                "[prefilter] re-encode failed (%s) — sending original wav", e,
            )
            tracer.note_section(
                "prefilter", {"reencode_error": str(e), "uploaded": "original"},
            )
            return wav_bytes
        tracer.attach("prefiltered.wav", out_wav)
        tracer.note_section("prefilter", {"output_audio": audio_stats(out_wav)})
        return out_wav

    def _load_silero(self) -> Optional[object]:
        """Get the process-wide Silero ONNX session, shared with the voice capture path."""
        session = shared_silero_session(_SILERO_MODEL_PATH)
        if session is None:
            logger.info("[prefilter] Silero unavailable — RMS-only prefilter")
            return None
        logger.info(
            "[prefilter] Silero VAD ready (threshold=%.2f, min_voiced=%.1fs)",
            PREFILTER_SILERO_THRESHOLD, PREFILTER_VAD_MIN_VOICED_S,
        )
        return session

    def _silero_voiced_seconds(
        self, samples: npt.NDArray[np.int16], sample_rate: int,
    ) -> Optional[float]:
        """Sum the duration of Silero-positive chunks across ``samples``.

        Silero is stateful; the LSTM ``state`` and the 64-sample ``context`` are local
        to this call and rebuilt from zeros each time, so independent prefilter
        invocations never bleed into each other.
        """
        if self._silero is None:
            return None
        chunk = PREFILTER_SILERO_CHUNK_SAMPLES
        ctx_len = PREFILTER_SILERO_CONTEXT_SAMPLES
        try:
            audio_norm = samples.astype(np.float32) / 32768.0
            state = np.zeros((2, 1, 128), dtype=np.float32)
            context = np.zeros((1, ctx_len), dtype=np.float32)
            voiced_chunks = 0
            total_chunks = 0
            chunk_s = chunk / float(sample_rate)
            total_available = -(-audio_norm.size // chunk)
            early_exit = False
            sr_arr = np.array(sample_rate, dtype=np.int64)
            for i in range(0, audio_norm.size, chunk):
                seg = audio_norm[i:i + chunk]
                if seg.size < chunk:
                    seg = np.pad(seg, (0, chunk - seg.size))
                x = np.concatenate([context, seg.reshape(1, -1)], axis=1)
                out = self._silero.run(
                    None,
                    {"input": x, "state": state, "sr": sr_arr},
                )
                prob = float(out[0][0][0])
                state = out[1]
                context = x[:, -ctx_len:]
                total_chunks += 1
                if prob >= PREFILTER_SILERO_THRESHOLD:
                    voiced_chunks += 1
                    if voiced_chunks * chunk_s >= PREFILTER_VAD_MIN_VOICED_S:
                        early_exit = True
                        break
            voiced_s = voiced_chunks * chunk_s
            logger.debug(
                "[prefilter] Silero stats: voiced_chunks=%d/%d voiced=%.2fs%s",
                voiced_chunks, total_chunks, voiced_s,
                (
                    f" (early exit at {total_chunks}/{total_available} chunks — "
                    f"voiced is a floor, not a total)" if early_exit else ""
                ),
            )
            tracer.note_section("prefilter", {
                "silero_voiced_chunks": voiced_chunks,
                "silero_total_chunks": total_chunks,
                "silero_early_exit": early_exit,
                "silero_chunks_available": total_available,
            })
            return voiced_s
        except Exception as e:
            logger.warning("[prefilter] Silero inference failed: %s", e)
            # SER-DEBUG: a Silero crash silently downgrades the gate to the
            # stricter RMS bar, which otherwise looks identical to "model not
            # installed" in the trace.
            tracer.note_section("prefilter", {"silero_error": str(e)})
            return None
