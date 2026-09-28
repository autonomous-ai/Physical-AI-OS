"""Gemini TTS backend: relay URL, voice/model fallback, SSE audio parsing."""

import base64
import json
from contextlib import contextmanager

from hal.drivers.voice.tts import PROVIDER_GEMINI, create_backend
from hal.drivers.voice.tts.gemini import GeminiTTSBackend


class _Resp:
    status_code = 200

    def iter_lines(self):
        for pcm in (b"\x01\x00" * 4, b"\x02\x00" * 4):
            part = {"inlineData": {"mimeType": "audio/l16; rate=24000", "data": base64.b64encode(pcm).decode()}}
            yield "data: " + json.dumps({"candidates": [{"content": {"parts": [part]}}]})
            yield ""


class _Client:
    def __init__(self):
        self.calls = []

    @contextmanager
    def stream(self, method, url, headers, json):
        self.calls.append((url, headers, json))
        yield _Resp()


def test_gemini_backend_streams_pcm_through_relay():
    backend = create_backend(PROVIDER_GEMINI, "k", "https://campaign-api.autonomous.ai/api/v1/ai")
    assert isinstance(backend, GeminiTTSBackend)
    backend._client = _Client()

    pcm = b"".join(backend.stream_pcm("[laughs] Hello", "nova", "tts-1", 1.0))

    assert pcm == b"\x01\x00" * 4 + b"\x02\x00" * 4
    url, headers, body = backend._client.calls[0]
    assert url == (
        "https://campaign-api.autonomous.ai/api/v1/ai/v1/google-search/v1beta/models/"
        f"{GeminiTTSBackend.DEFAULT_MODEL}:streamGenerateContent?alt=sse"
    )
    assert headers["x-goog-api-key"] == "k"
    # Re-feeding _base_url (as routes/voice.py does on config apply) must not stack the relay path.
    assert create_backend(PROVIDER_GEMINI, "k", backend._base_url)._base_url == backend._base_url
    assert body["contents"][0]["parts"][0]["text"] == "Hello"
    voice = body["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"]
    assert voice == GeminiTTSBackend.DEFAULT_VOICE


def test_gemini_backend_skips_tag_only_text():
    backend = GeminiTTSBackend("k", "https://x/api/v1/ai/v1")
    backend._client = _Client()
    assert list(backend.stream_pcm("[sigh]", "Kore", "gemini-3.8-flash-tts", 1.0)) == []
    assert backend._client.calls == []
