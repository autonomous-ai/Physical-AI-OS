"""ElevenLabs TTS backend over WebSocket (stream-input protocol)."""

import base64
import json
import logging
import os
from typing import Iterator, Optional

from hal.drivers.voice.tts.backend import TTSBackend, TTSRateLimitError
from hal.drivers.voice.tts.elevenlabs import ElevenLabsTTSBackend
from hal.drivers.voice.tts.openai import _ensure_openai_v1

logger = logging.getLogger("hal.voice.tts")


class ElevenLabsWSTTSBackend(TTSBackend):
    """ElevenLabs TTS over the stream-input WebSocket."""

    DEFAULT_MODEL = "eleven_flash_v2_5"
    ELEVENLABS_PATH = ElevenLabsTTSBackend.ELEVENLABS_PATH
    VOICE_IDS = ElevenLabsTTSBackend.VOICE_IDS

    def __init__(self, api_key: str, base_url: Optional[str] = None):
        self._api_key = api_key
        http_base = _ensure_openai_v1(base_url or "")
        ws_root = (
            http_base.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        )
        self._url_tmpl = os.environ.get("HAL_TTS_ELEVENLABS_WS_URL", "").strip() or (
            ws_root + "/ws/elevenlabs/text-to-speech/{voice_id}/stream-input"
            "?model_id={model}&output_format=pcm_24000"
        )
        self._connect = None
        try:
            from websockets.sync.client import connect

            self._connect = connect
            logger.info("ElevenLabs WS TTS backend ready (url_tmpl=%s)", self._url_tmpl)
        except ImportError as e:
            logger.warning("websockets not available for ElevenLabs WS backend: %s", e)

    @property
    def available(self) -> bool:
        return self._connect is not None and bool(self._api_key)

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
    ) -> Iterator[bytes]:
        el_model = model if model.startswith("eleven_") else self.DEFAULT_MODEL
        voice_id = self.VOICE_IDS.get(voice, voice)
        url = self._url_tmpl.format(voice_id=voice_id, model=el_model)

        bos: dict = {"text": " ", "xi_api_key": self._api_key}
        bos["voice_settings"] = {"speed": max(0.7, min(1.2, speed))}

        try:
            ws = self._connect(
                url,
                additional_headers={"xi-api-key": self._api_key},
                open_timeout=10,
                close_timeout=5,
            )
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None) or getattr(
                e, "status_code", None
            )
            if status == 429:
                raise TTSRateLimitError(
                    f"ElevenLabs WS rate limit (handshake {status})", status_code=status
                ) from e
            raise
        try:
            ws.send(json.dumps(bos))
            ws.send(json.dumps({"text": text}))
            ws.send(json.dumps({"text": ""}))
            for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                err = msg.get("error") or msg.get("message")
                if err and any(k in str(err).lower() for k in ("quota", "rate limit", "too many", "usage limit")):
                    raise TTSRateLimitError(f"ElevenLabs WS rate limit: {err}", status_code=429)
                audio_b64 = msg.get("audio")
                if audio_b64:
                    yield base64.b64decode(audio_b64)
                if msg.get("isFinal"):
                    break
        finally:
            try:
                ws.close()
            except Exception:
                pass
