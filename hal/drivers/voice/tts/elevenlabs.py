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

# Audio direction tags like "[laughs]" — eleven_v3 interprets them, but a chunk
# with ONLY tags (no words) has no speakable content and ElevenLabs 400s it.
_AUDIO_TAG_RE = re.compile(r"\[[^\]]*\]")


class ElevenLabsTTSBackend(TTSBackend):
    """ElevenLabs TTS backend with streaming support."""

    DEFAULT_MODEL = "eleven_v3"
    cache_revision = "local-v3-tempo-v1"
    supports_synthesis_cancellation = True
    ELEVENLABS_PATH = "/elevenlabs"

    # Voice name -> voice_id mapping, grouped by trained language.
    # Curated for companion AI — warm, friendly, expressive. Top picks marked (*).
    #
    # eleven_v3 is multilingual — every voice can technically speak any
    # language — but voices trained on a language sound substantially more
    # natural in that language (accent, prosody). The web UI filters this
    # by stt_language so VN/CN owners don't have to scroll past 22 American
    # voices to find one that fits.
    #
    # zh-CN and zh-TW share the same voice pool — script (Simplified vs
    # Traditional) differs in TEXT, not in speaker. Jin has a Taiwan accent
    # which the web UI may want to surface for zh-TW pickers.
    # "zh" below is an internal meta-bucket (not a stt_language code) —
    # both LANG_ZH_CN and LANG_ZH_TW share this voice pool since
    # voice IDs are script-agnostic.
    _LANG_BUCKET_ZH = "zh"

    VOICE_IDS_BY_LANG = {
        LANG_EN: {
            # Female — premade
            "Rachel": "21m00Tcm4TlvDq8ikWAM",       # (*) warm, natural American
            "Sarah": "EXAVITQu4vr4xnSDxMaL",        # (*) friendly, clear American
            "Nicole": "piTKgcLEGmPE4e6mEKli",       # soft, inspirational
            # Female — community (conversational, young, American)
            "Terra": "aFueGIISJUmscc05ZNfD",         # (*) bubbly, friendly — 14k clones
            "Maria": "vZzlAds9NzvLsFSWp0qk",        # (*) soft, calm, expressive — 48k clones
            "Sophie": "AEW6JTgnyoPaoB9zlK3S",       # (*) sparky, energetic, young
            "Piper": "rzgrf9VyEb0LLa824k8Q",        # spirited, upbeat, dynamic
            "Mia": "052jzHJceQiZr7ltnY0C",          # lively, warm, expressive
            "Kimmy": "TmK7x2BFDD7TOVlR69J2",        # youthful, sweet, natural charm
            "Brianna": "2NzqTfQARqdn4tcBKTSh",      # soft, sincere, intimate
            "Ally": "qmm0vRXCIew16ilYAeiI",         # bubbly, fun, caring
            "Tori": "lAxf5ma5HGtzxC434SWT",         # confident, warm, encouraging
            # Male — premade
            "Brian": "nPczCjzI2devNBz1zQrb",        # (*) cheerful, relatable American
            "Adam": "pNInz6obpgDQGcFmaJgB",         # (*) warm, emotional depth
            "Daniel": "onwK4e9ZLuTAKqWW03F9",       # (*) well-paced, clear
            "George": "JBFqnCBsd6RMkjVDRZzb",
            "James": "ZQe5CZNOzWyzPSCn5a3c",        # calm British
            "Liam": "TX3LPaxmHKxFdv7VOQHJ",        # energetic American
            "Charlie": "IKne3meq5aSn9XLyUdCD",
            "Sam": "yoZ06aMxZJJ28mfd3POQ",
            # Male — community (conversational, young, American)
            "Sean": "FgARTjeugpFkVodK0Ovq",         # (*) casual, optimized for conversation — 1.9k clones
            "Kael": "RxsTyZQJnPygpas5IyzL",         # (*) energetic, trendy, youthful — 1.8k clones
            "Brooks": "sUzXYdokj3o9QQ91yPRF",       # (*) bright, affable, friendly smile — 1.5k clones
            "Erion": "BSgaLWMIhbNhOCIH1apf",        # unique, friendly, casual — 1.3k clones
        },
        LANG_VI: {
            "Ngan": "a3AkyqGG4v8Pg7SWQ0Y3",         # (*) Female, bubbly, friendly, authentic
            "Linh": "L5c6tGA8OiORYKxez5Zu",         # (*) Female, soft, calm, beautifully expressive
            "Huyen": "foH7s9fX31wFFH2yqrFa",        # Female, calm, friendly, clear (Da Nang)
            "Freya": "rXOGzMiqbmjugMpzKMEx",        # Female, young, soft, cute (Northern accent)
            "Nathan": "u8EWWYyBDfXFxHak7WM3",       # (*) Male, soft-spoken, gentle, sing-song Central
            "Quan": "puBBfOSRT9Dbk3FUJQGd",         # Male, warm, thoughtful Central tone
        },
        _LANG_BUCKET_ZH: {
            "Amy": "bhJUNIXWQQ94l8eI2VUf",          # (*) Female, relaxed, friendly Beijing tone
            "Sage": "APSIkVZudNbPAwyPoeVO",         # (*) Female, warm, soothing narrative voice
            "Xiaoxi": "9DMBSOAnMDPiFAsz1ZGK",       # Female, neutral, friendly, approachable
            "Yun": "YxbjaPemDJV2xlfvkiIG",          # Female, elegant, sweet, gentle
            "Evan Zhao": "MI36FIkp9wRP7cpWKPTl",    # (*) Male, calm, trustworthy, warm
            "Jin": "vZZLclMx4wouUtKBRfZn",          # Male, casual, Taiwan-influenced (good for zh-TW)
        },
    }

    # Flat name -> voice_id lookup, derived from VOICE_IDS_BY_LANG. Keeps
    # the resolve path self.VOICE_IDS.get(voice, voice) cheap and unaware
    # of language, so any saved tts_voice still resolves regardless of
    # which language the owner is currently set to.
    VOICE_IDS = {
        name: vid
        for lang_voices in VOICE_IDS_BY_LANG.values()
        for name, vid in lang_voices.items()
    }

    @classmethod
    def voices_for_language(cls, lang: str) -> list:
        """Return curated voice names for a given stt_language code.
        Empty / unknown lang → flat list of all voices (so the picker
        keeps working when stt_language is "auto"). zh-CN and zh-TW
        both map to the shared "zh" bucket since voice IDs are
        script-agnostic."""
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

    # Direct ElevenLabs API hosts. The api.us / api.eu splits are the
    # regional endpoints ElevenLabs docs advertise; everything else
    # (autonomous.ai proxy, private mirrors) still gets the `/elevenlabs`
    # prefix so proxy routing keeps working.
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
        # The `/elevenlabs` sub-path is autonomous.ai proxy routing
        # (campaign-api routes `<base>/elevenlabs/text-to-speech/...` to the
        # provider). A BYO operator pointing directly at api.elevenlabs.io
        # should NOT get the extra prefix — the real API is
        # `<base>/text-to-speech/...`, and appending `/elevenlabs` gives a 404
        # ("/v1/elevenlabs/text-to-speech/..."). Detect the direct-API host
        # and skip the prefix.
        resolved = _ensure_openai_v1(base_url or "")
        if self._is_direct_elevenlabs(resolved):
            self._base_url = resolved
        else:
            self._base_url = resolved + self.ELEVENLABS_PATH
        self._client = None
        try:
            import httpx
            # Persistent client reuses TCP/TLS across speaks -- saves ~100-500ms per
            # call vs httpx.stream() module-level which builds a fresh Client+TLS each time.
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
        # Resolve voice name to voice_id (pass through if already an ID)
        voice_id = self.VOICE_IDS.get(voice, voice)
        # Skip non-speakable chunks: ElevenLabs 400s on empty / whitespace-only /
        # audio-tag-only text (sentence splitting can emit a bare "[laughs]" or a
        # trailing empty fragment). No words = nothing to synth — yield nothing
        # instead of a failed request. Device-confirmed: "", " ", "[laughs]" → 400.
        if not _AUDIO_TAG_RE.sub("", text or "").strip():
            logger.debug("ElevenLabs TTS: skipping non-speakable chunk: %r", text)
            return
        # output_format is a query param, not body — pcm_24000 = 24kHz 16-bit mono
        url = f"{self._base_url}/text-to-speech/{voice_id}/stream?output_format=pcm_24000"
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        body = {
            "text": text,
            "model_id": el_model,
        }
        # Send normal speed explicitly too: omission inherits the voice's
        # stored settings, which may use a different speaking speed.
        local_tempo = el_model == "eleven_v3"
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
                    # raise_for_status alone swallows the body — log the real reason
                    # + the offending text so the next 400 (other cause) is diagnosable.
                    try:
                        detail = response.read().decode(errors="replace")[:300]
                    except Exception:
                        detail = "<unreadable>"
                    logger.error(
                        "ElevenLabs TTS %d voice=%s model=%s speed=%s text=%r: %s",
                        response.status_code, voice_id, el_model, speed, text[:80], detail,
                    )
                    # Rate limit (429) or quota exhausted (401/402 with a quota body):
                    # raise a dedicated error so the service announces it to the user
                    # via a prerendered notice instead of retrying pointlessly and
                    # failing silently. 401 alone is usually a bad key, so only treat
                    # it as quota when the body says so.
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
                    # Preserve the previous 4096-byte output boundaries while observing
                    # bytes before HTTPX's application-level chunk accumulation.
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

        # Start the filter before the HTTP generator is consumed. ffmpeg startup
        # overlaps network/provider wait rather than delaying the first PCM bytes.
        chunks = fetch_chunks()
        if local_tempo:
            logger.info("TTS v3 local tempo: speed=%.2f provider_speed=1.00", speed)
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
