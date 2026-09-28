"""Text-to-speech (TTS) package: service + pluggable backends.

Public surface:
    TTSService            — synthesize + play speech (voice_service / routes use this)
    create_backend        — factory: provider name → backend instance
    TTSBackend            — backend ABC
    ElevenLabsTTSBackend / OpenAITTSBackend — concrete backends
    PROVIDER_OPENAI / PROVIDER_ELEVENLABS / PROVIDER_GEMINI — provider id constants
    TTS_SAMPLE_RATE / STREAM_CHUNK_SIZE     — audio constants
"""

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
