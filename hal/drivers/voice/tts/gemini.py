"""Gemini TTS backend (generateContent with AUDIO modality, SSE streaming)."""

import base64
import json
import logging
import os
from typing import Iterator, Optional

from hal.drivers.voice.tts.backend import PROVIDER_GEMINI, TTSBackend, TTSRateLimitError
from hal.drivers.voice.tts.openai import OpenAITTSBackend, _ensure_openai_v1
from hal.drivers.voice.tts.tempo import change_tempo

logger = logging.getLogger("hal.voice.tts")


class GeminiTTSBackend(TTSBackend):
    """Gemini TTS through the autonomous proxy's Gemini REST relay."""

    DEFAULT_MODEL = os.environ.get("HAL_TTS_GEMINI_MODEL", "gemini-3.8-flash-tts")
    DEFAULT_VOICE = "Kore"
    RELAY_PATH = "/google-search/v1beta/models"
    supports_synthesis_cancellation = True

    VOICES = [
        "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
        "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
        "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
        "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird",
        "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
    ]

    def __init__(self, api_key: str, base_url: Optional[str] = None):
        self._api_key = api_key
        self._base_url = _ensure_openai_v1(base_url or "")
        self._client = None
        try:
            import httpx
            self._client = httpx.Client(
                timeout=30.0,
                limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=300.0),
            )
            logger.info("Gemini TTS backend ready (relay=%s, model=%s)", self._base_url + self.RELAY_PATH, self.DEFAULT_MODEL)
        except ImportError as e:
            logger.warning("httpx not available for Gemini backend: %s", e)

    @property
    def available(self) -> bool:
        return self._client is not None and bool(self._api_key)

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
        text = OpenAITTSBackend._strip_audio_tags(text or "")
        if not text:
            return
        gm_model = model if model.startswith("gemini-") and "tts" in model else self.DEFAULT_MODEL
        gm_voice = voice if voice in self.VOICES else self.DEFAULT_VOICE
        url = f"{self._base_url}{self.RELAY_PATH}/{gm_model}:streamGenerateContent?alt=sse"
        body = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": gm_voice}}},
            },
        }
        headers = {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

        def fetch_chunks():
            with self._client.stream("POST", url, headers=headers, json=body) as response:
                if response.status_code >= 400:
                    try:
                        detail = response.read().decode(errors="replace")[:300]
                    except Exception:
                        detail = "<unreadable>"
                    logger.error("Gemini TTS %d voice=%s model=%s text=%r: %s",
                                 response.status_code, gm_voice, gm_model, text[:80], detail)
                    if response.status_code == 429:
                        raise TTSRateLimitError(f"Gemini TTS rate limit / quota: {detail}", status_code=429)
                    response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:])
                    for cand in event.get("candidates") or []:
                        for part in (cand.get("content") or {}).get("parts") or []:
                            data = (part.get("inlineData") or {}).get("data")
                            if data:
                                yield base64.b64decode(data)

        chunks = change_tempo(fetch_chunks(), speed, self.sample_rate,
                              **({"cancelled": cancelled} if cancelled is not None else {}))
        try:
            yield from chunks
        finally:
            chunks.close()


def native_voice(tts) -> Optional[str]:
    """Voice Gemini Live should speak natively, or None to keep our TTS."""
    import hal.config as hal_config
    if tts is None or getattr(tts, "_provider", "") != PROVIDER_GEMINI:
        return None
    if hal_config.REALTIME_PROVIDER.strip().lower() != "gemini":
        return None
    voice = getattr(tts, "_voice", "")
    return voice if voice in GeminiTTSBackend.VOICES else GeminiTTSBackend.DEFAULT_VOICE
