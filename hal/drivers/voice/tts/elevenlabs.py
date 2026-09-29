"""ElevenLabs TTS backend with streaming support."""

import logging
import hashlib
import re
import time
import uuid
from typing import Iterator, Optional
from urllib.parse import urlparse

from hal.presets import LANG_EN, LANG_VI
from hal.drivers.voice.tts.backend import (
    TTSBackend,
    STREAM_CHUNK_SIZE,
    TTSRateLimitError,
)
from hal.drivers.voice.tts.openai import _ensure_openai_v1
from hal.drivers.voice.tts.tempo import change_tempo

logger = logging.getLogger("hal.voice.tts")

# Chunks with only audio tags (e.g. "[laughs]") have no speakable text; ElevenLabs 400s them.
_AUDIO_TAG_RE = re.compile(r"\[[^\]]*\]")


class ElevenLabsTTSBackend(TTSBackend):
    """ElevenLabs TTS backend with streaming support."""

    DEFAULT_MODEL = "eleven_v4_turbo"
    cache_revision = "default-v4-turbo-local-tempo-v1"
    supports_synthesis_cancellation = True
    ELEVENLABS_PATH = "/elevenlabs"

    # Voice name -> voice_id mapping, grouped by trained language. The web UI filters
    # this by stt_language so VN/CN owners don't have to scroll past 22 American voices
    # to find one that fits.
    # "zh" is an internal bucket (not a stt_language code) shared by zh-CN and zh-TW.
    _LANG_BUCKET_ZH = "zh"

    VOICE_IDS_BY_LANG = {
        LANG_EN: {
            "Rachel": "21m00Tcm4TlvDq8ikWAM",
            "Sarah": "EXAVITQu4vr4xnSDxMaL",
            "Nicole": "piTKgcLEGmPE4e6mEKli",
            "Terra": "aFueGIISJUmscc05ZNfD",
            "Maria": "vZzlAds9NzvLsFSWp0qk",
            "Sophie": "AEW6JTgnyoPaoB9zlK3S",
            "Piper": "rzgrf9VyEb0LLa824k8Q",
            "Mia": "052jzHJceQiZr7ltnY0C",
            "Kimmy": "TmK7x2BFDD7TOVlR69J2",
            "Brianna": "2NzqTfQARqdn4tcBKTSh",
            "Ally": "qmm0vRXCIew16ilYAeiI",
            "Tori": "lAxf5ma5HGtzxC434SWT",
            "Brian": "nPczCjzI2devNBz1zQrb",
            "Adam": "pNInz6obpgDQGcFmaJgB",
            "Daniel": "onwK4e9ZLuTAKqWW03F9",
            "George": "JBFqnCBsd6RMkjVDRZzb",
            "James": "ZQe5CZNOzWyzPSCn5a3c",
            "Liam": "TX3LPaxmHKxFdv7VOQHJ",
            "Charlie": "IKne3meq5aSn9XLyUdCD",
            "Sam": "yoZ06aMxZJJ28mfd3POQ",
            "Sean": "FgARTjeugpFkVodK0Ovq",
            "Kael": "RxsTyZQJnPygpas5IyzL",
            "Brooks": "sUzXYdokj3o9QQ91yPRF",
            "Erion": "BSgaLWMIhbNhOCIH1apf",
        },
        LANG_VI: {
            "Ngan": "a3AkyqGG4v8Pg7SWQ0Y3",
            "Linh": "L5c6tGA8OiORYKxez5Zu",
            "Huyen": "foH7s9fX31wFFH2yqrFa",
            "Freya": "rXOGzMiqbmjugMpzKMEx",
            "Nathan": "u8EWWYyBDfXFxHak7WM3",
            "Quan": "puBBfOSRT9Dbk3FUJQGd",
        },
        _LANG_BUCKET_ZH: {
            "Amy": "bhJUNIXWQQ94l8eI2VUf",
            "Sage": "APSIkVZudNbPAwyPoeVO",
            "Xiaoxi": "9DMBSOAnMDPiFAsz1ZGK",
            "Yun": "YxbjaPemDJV2xlfvkiIG",
            "Evan Zhao": "MI36FIkp9wRP7cpWKPTl",
            "Jin": "vZZLclMx4wouUtKBRfZn",
        },
    }

    VOICE_IDS = {
        name: vid
        for lang_voices in VOICE_IDS_BY_LANG.values()
        for name, vid in lang_voices.items()
    }

    @classmethod
    def voices_for_language(cls, lang: str) -> list:
        """Return curated voice names for a given stt_language code."""
        if not lang:
            return list(cls.VOICE_IDS.keys())
        bucket = LANG_EN
        if lang.startswith(LANG_VI):
            bucket = LANG_VI
        elif lang.startswith(cls._LANG_BUCKET_ZH):
            bucket = cls._LANG_BUCKET_ZH
        elif lang.startswith(LANG_EN):
            bucket = LANG_EN
        pool = cls.VOICE_IDS_BY_LANG.get(bucket)
        if not pool:
            pool = cls.VOICE_IDS_BY_LANG[LANG_EN]
        return list(pool.keys())

    _DIRECT_HOSTS = frozenset({
        "api.elevenlabs.io",
        "api.us.elevenlabs.io",
        "api.eu.elevenlabs.io",
    })

    @classmethod
    def _is_direct_elevenlabs(cls, base_url: str) -> bool:
        try:
            host = urlparse(base_url).hostname or ""
        except Exception:
            return False
        return host.lower() in cls._DIRECT_HOSTS

    def __init__(self, api_key: str, base_url: Optional[str] = None):
        self._api_key = api_key
        resolved = _ensure_openai_v1(base_url or "")
        if self._is_direct_elevenlabs(resolved):
            self._base_url = resolved
        else:
            self._base_url = resolved + self.ELEVENLABS_PATH
        self._client = None
        try:
            import httpx
            self._client = httpx.Client(
                timeout=30.0,
                limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=300.0),
            )
            logger.info("ElevenLabs TTS backend ready (proxy=%s)", self._base_url)
        except ImportError as e:
            logger.warning("httpx not available for ElevenLabs backend: %s", e)

    @property
    def available(self) -> bool:
        return self._client is not None and bool(self._api_key)

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    @property
    def volume_boost(self) -> float:
        return 1.0

    def stream_pcm(
        self,
        text: str,
        voice: str,
        model: str,
        speed: float,
        instructions: Optional[str] = None,
        cancelled=None,
    ) -> Iterator[bytes]:
        el_model = model if model.startswith("eleven_") else self.DEFAULT_MODEL
        voice_id = self.VOICE_IDS.get(voice, voice)
        if not _AUDIO_TAG_RE.sub("", text or "").strip():
            logger.debug("ElevenLabs TTS: skipping non-speakable chunk: %r", text)
            return
        url = f"{self._base_url}/text-to-speech/{voice_id}/stream?output_format=pcm_24000"
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        body = {
            "text": text,
            "model_id": el_model,
        }
        # v3/v4 get speed 1.0 explicitly (omitting it inherits the voice's stored speed); tempo is applied locally.
        local_tempo = el_model in ("eleven_v3", "eleven_v4", "eleven_v4_turbo")
        body["voice_settings"] = {
            "speed": 1.0 if local_tempo else max(0.7, min(1.2, speed)),
        }

        timing_id = uuid.uuid4().hex[:12]
        text_key = hashlib.sha256(text.encode()).hexdigest()[:12]
        started_at = time.perf_counter()
        def fetch_chunks():
            nonlocal started_at
            started_at = time.perf_counter()
            logger.info("[tts-timing] stage=http_start request=%s text_key=%s chars=%d model=%s speed=%.2f",
                        timing_id, text_key, len(text), el_model, speed)
            with self._client.stream(
                "POST", url, headers=headers, json=body
            ) as response:
                logger.info("[tts-timing] stage=http_headers request=%s elapsed_ms=%.1f status=%d",
                            timing_id, (time.perf_counter() - started_at) * 1000, response.status_code)
                if response.status_code >= 400:
                    try:
                        detail = response.read().decode(errors="replace")[:300]
                    except Exception:
                        detail = "<unreadable>"
                    logger.error(
                        "ElevenLabs TTS %d voice=%s model=%s speed=%s text=%r: %s",
                        response.status_code, voice_id, el_model, speed, text[:80], detail,
                    )
                    if response.status_code == 429 or (
                        response.status_code in (401, 402)
                        and "quota" in detail.lower()
                    ):
                        raise TTSRateLimitError(
                            f"ElevenLabs rate limit / quota: {detail}",
                            status_code=response.status_code,
                        )
                    response.raise_for_status()
                def measured_chunks():
                    pending = bytearray()
                    first_network = True
                    first_buffered = True
                    for raw in response.iter_bytes():
                        if not raw:
                            continue
                        if first_network:
                            first_network = False
                            logger.info("[tts-timing] stage=http_first_bytes request=%s elapsed_ms=%.1f bytes=%d",
                                        timing_id, (time.perf_counter() - started_at) * 1000, len(raw))
                        pending.extend(raw)
                        while len(pending) >= STREAM_CHUNK_SIZE:
                            chunk = bytes(pending[:STREAM_CHUNK_SIZE])
                            del pending[:STREAM_CHUNK_SIZE]
                            if first_buffered:
                                first_buffered = False
                                logger.info("[tts-timing] stage=buffer_first_chunk request=%s elapsed_ms=%.1f bytes=%d",
                                            timing_id, (time.perf_counter() - started_at) * 1000, len(chunk))
                            yield chunk
                    if pending:
                        if first_buffered:
                            logger.info("[tts-timing] stage=buffer_first_chunk request=%s elapsed_ms=%.1f bytes=%d",
                                        timing_id, (time.perf_counter() - started_at) * 1000, len(pending))
                        yield bytes(pending)

                yield from measured_chunks()

        chunks = fetch_chunks()
        if local_tempo:
            logger.info("TTS %s local tempo: speed=%.2f provider_speed=1.00", el_model, speed)
            chunks = change_tempo(chunks, speed, self.sample_rate,
                                  **({"cancelled": cancelled} if cancelled is not None else {}))
        try:
            first_output = True
            for chunk in chunks:
                if first_output:
                    first_output = False
                    logger.info("[tts-timing] stage=tempo_first_output request=%s elapsed_ms=%.1f bytes=%d",
                                timing_id, (time.perf_counter() - started_at) * 1000, len(chunk))
                yield chunk
        finally:
            chunks.close()
