"""Text-to-speech (TTS) package: service + pluggable backends."""

from hal.drivers.voice.tts.backend import (
    PROVIDER_ELEVENLABS,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    STREAM_CHUNK_SIZE,
    TTS_SAMPLE_RATE,
    TTSBackend,
    create_backend,
)
from hal.drivers.voice.tts.elevenlabs import ElevenLabsTTSBackend
from hal.drivers.voice.tts.openai import OpenAITTSBackend, _ensure_openai_v1
from hal.drivers.voice.tts.service import TTSService

__all__ = [
    "ElevenLabsTTSBackend",
    "OpenAITTSBackend",
    "PROVIDER_ELEVENLABS",
    "PROVIDER_GEMINI",
    "PROVIDER_OPENAI",
    "STREAM_CHUNK_SIZE",
    "TTS_SAMPLE_RATE",
    "TTSBackend",
    "TTSService",
    "_ensure_openai_v1",
    "create_backend",
]
