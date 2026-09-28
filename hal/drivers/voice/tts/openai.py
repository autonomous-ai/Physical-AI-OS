"""OpenAI-compatible TTS backend."""

import logging
import re
from typing import Iterator, Optional

from hal.drivers.voice.tts.backend import (
    TTSBackend,
    STREAM_CHUNK_SIZE,
    TTSRateLimitError,
)

logger = logging.getLogger("hal.voice.tts")


def _ensure_openai_v1(base_url: str) -> str:
    """Append /v1 to autonomous API base URLs that are missing it."""
    base_url = base_url.rstrip("/")
    if "campaign-api.autonomous.ai" in base_url and base_url.endswith("/ai"):
        base_url += "/v1"
    return base_url


class OpenAITTSBackend(TTSBackend):
    """OpenAI-compatible TTS backend (default)."""

    def __init__(self, api_key: str, base_url: str):
        self._client = None
        try:
            from openai import OpenAI
            base_url = _ensure_openai_v1(base_url)
            # timeout=10 caps a single TTS request end-to-end.
            self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=10.0)
            logger.info("OpenAI TTS backend ready (base_url=%s, timeout=10s)", base_url)
        except ImportError as e:
            logger.warning("openai SDK not available: %s", e)

    @property
    def available(self) -> bool:
        return self._client is not None

    @staticmethod
    def _strip_audio_tags(text: str) -> str:
        """Remove ElevenLabs-style audio tags like [laugh], [sigh] etc."""
        return re.sub(r'\[(?:laugh|sigh|whisper|gasp|gulp|nervous|excited|frustrated|sorrowful|calm)[^\]]*\]', '', text, flags=re.IGNORECASE).strip()

    def stream_pcm(
        self,
        text: str,
        voice: str,
        model: str,
        speed: float,
        instructions: Optional[str] = None,
    ) -> Iterator[bytes]:
        text = self._strip_audio_tags(text)
        if not text:
            return
        kwargs = dict(
            model=model,
            voice=voice,
            input=text,
            response_format="pcm",
            speed=speed,
        )
        if instructions:
            kwargs["instructions"] = instructions
        try:
            with self._client.audio.speech.with_streaming_response.create(**kwargs) as response:
                for chunk in response.iter_bytes(STREAM_CHUNK_SIZE):
                    yield chunk
        except Exception as e:
            # OpenAI SDK raises openai.RateLimitError (an APIStatusError with
            # status_code 429) for rate limit AND insufficient_quota. Match on
            # status_code so we don't need a hard import of the SDK's exception types
            # here.
            if getattr(e, "status_code", None) == 429:
                raise TTSRateLimitError(f"OpenAI TTS rate limit / quota: {e}", status_code=429) from e
            raise
