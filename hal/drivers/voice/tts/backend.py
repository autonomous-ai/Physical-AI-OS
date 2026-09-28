"""
TTS Backend abstraction — pluggable providers for text-to-speech streaming.

Supported providers:
  - openai (default): OpenAI-compatible API (works with any OpenAI-compatible server)
  - elevenlabs: ElevenLabs TTS API with streaming support
  - piper: on-device synthesis, no network and no shared rate limit
  - gemini: Gemini TTS models through the autonomous proxy's Gemini REST relay
"""

import logging
from abc import ABC, abstractmethod
from typing import Iterator, Optional

logger = logging.getLogger("hal.voice.tts")

# Provider constants
PROVIDER_OPENAI = "openai"
PROVIDER_ELEVENLABS = "elevenlabs"
PROVIDER_PIPER = "piper"
PROVIDER_GEMINI = "gemini"

# All backends output 24kHz 16-bit mono PCM
TTS_SAMPLE_RATE = 24000
STREAM_CHUNK_SIZE = 4096


class TTSRateLimitError(Exception):
    """Raised by a backend when the provider rejects the request for rate-limit
    or quota reasons (HTTP 429, or 401/402 quota-exhausted). Distinct from other
    HTTP errors so the service can announce it to the user (prerendered notice)
    instead of failing silently, and skip pointless retries. Carries
    `status_code` so the retry loops can match on it like other HTTP errors."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class TTSBackend(ABC):
    """Abstract TTS backend — streams raw PCM int16 bytes from text."""

    @abstractmethod
    def stream_pcm(
        self,
        text: str,
        voice: str,
        model: str,
        speed: float,
        instructions: Optional[str] = None,
    ) -> Iterator[bytes]:
        """Yield raw PCM int16 byte chunks (24kHz mono) for the given text."""
        ...

    @property
    def sample_rate(self) -> int:
        return TTS_SAMPLE_RATE

    @property
    def volume_boost(self) -> float:
        """Volume multiplier applied to PCM samples. Override per provider."""
        return 2.5

    @property
    @abstractmethod
    def available(self) -> bool:
        ...


def create_backend(
    provider: str,
    api_key: str,
    base_url: str = "",
) -> TTSBackend:
    """Factory: create a TTS backend by provider name."""
    provider = (provider or PROVIDER_OPENAI).lower().strip()
    if provider == PROVIDER_PIPER:
        # No api_key or base_url: synthesis is local, so neither applies.
        from hal.drivers.voice.tts.piper import PiperTTSBackend
        return PiperTTSBackend()
    if provider == PROVIDER_GEMINI:
        from hal.drivers.voice.tts.gemini import GeminiTTSBackend
        return GeminiTTSBackend(api_key=api_key, base_url=base_url)
    if provider == PROVIDER_ELEVENLABS:
        # WebSocket (stream-input) variant behind a flag; default is HTTP.
        import hal.config as _cfg
        if getattr(_cfg, "TTS_ELEVENLABS_WS", False):
            from hal.drivers.voice.tts.elevenlabs_ws import ElevenLabsWSTTSBackend
            logger.info("ElevenLabs TTS: using WebSocket backend (HAL_TTS_ELEVENLABS_WS=true)")
            return ElevenLabsWSTTSBackend(api_key=api_key, base_url=base_url)
        from hal.drivers.voice.tts.elevenlabs import ElevenLabsTTSBackend
        return ElevenLabsTTSBackend(api_key=api_key, base_url=base_url)
    # Default: openai-compatible
    from hal.drivers.voice.tts.openai import OpenAITTSBackend
    return OpenAITTSBackend(api_key=api_key, base_url=base_url)
