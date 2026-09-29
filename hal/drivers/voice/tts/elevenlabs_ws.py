"""ElevenLabs TTS backend over WebSocket (stream-input protocol).

Drop-in alternative to the HTTP ElevenLabsTTSBackend, selected when
HAL_TTS_ELEVENLABS_WS=true. Implements the SAME `stream_pcm()` contract
(yield raw PCM int16 bytes, 24 kHz mono) so VoiceService / TTSService need no
changes — only `create_backend` picks the variant.

Protocol (ElevenLabs `stream-input`, proxied by campaign-api):
  connect  wss://<base>/elevenlabs/text-to-speech/<voice_id>/stream-input
             ?model_id=<model>&output_format=pcm_24000
  send     {"text": " ", "voice_settings": {...}, "xi_api_key": "<key>"}   (BOS)
  send     {"text": "<full text> "}
  send     {"text": ""}                                                    (EOS/flush)
  recv ... {"audio": "<base64 pcm>", "isFinal": true|null, ...}            (loop)

We have the full sentence up front (TTSService already chunks text), so one
text message + EOS per call; we open a fresh socket per utterance (stream-input
is one synthesis per BOS→EOS cycle). The api key is sent BOTH as a connect
header and in the BOS message so it works whichever the proxy expects.
"""

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
    """ElevenLabs TTS over the stream-input WebSocket. Same output as the HTTP
    backend: raw PCM int16, 24 kHz mono, volume_boost 1.0."""

    # Keep this stream-input backend separate from the v4 HTTP default.
    # v3 previously failed here with upstream 1011 "Failed to connect to
    # ElevenLabs service". stream-input takes the turbo/flash/
    # multilingual families; flash_v2_5 is low-latency + multilingual (good for VI).
    DEFAULT_MODEL = "eleven_flash_v2_5"
    ELEVENLABS_PATH = ElevenLabsTTSBackend.ELEVENLABS_PATH
    # Reuse the HTTP backend's name→voice_id table so saved voices resolve identically.
    VOICE_IDS = ElevenLabsTTSBackend.VOICE_IDS

    def __init__(self, api_key: str, base_url: Optional[str] = None):
        self._api_key = api_key
        http_base = _ensure_openai_v1(base_url or "")
        ws_root = (
            http_base.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        )
        # Proxy WS endpoints live under /ws/ (cf. STT /ws/audio/transcriptions,
        # Gemini /ws/gemini), NOT at the HTTP REST path. The exact ElevenLabs WS
        # path/shape is BE-specific — override the FULL url via
        # HAL_TTS_ELEVENLABS_WS_URL (placeholders {voice_id} {model}) when it
        # differs from this default guess. Lets us retune the endpoint with an
        # .env change + restart, no code redeploy.
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
        # Override stored voice settings even when normal speed is selected.
        bos["voice_settings"] = {"speed": max(0.7, min(1.2, speed))}

        try:
            ws = self._connect(
                url,
                additional_headers={"xi-api-key": self._api_key},
                open_timeout=10,
                close_timeout=5,
            )
        except Exception as e:
            # websockets raises InvalidStatus(.response.status_code) on a non-101
            # handshake; a 429 there = rate limit / quota. Surface it distinctly
            # so the service can announce the prerendered notice.
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
            ws.send(json.dumps({"text": ""}))  # EOS — flush + close the turn
            for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                # Proxy relays quota/limit rejections as an error message rather
                # than an HTTP status once the socket is open.
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
