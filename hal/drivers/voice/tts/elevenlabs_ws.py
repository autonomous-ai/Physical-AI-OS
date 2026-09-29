"""ElevenLabs v4 Turbo over Text to Dialogue WebSocket.

Selected by HAL_TTS_ELEVENLABS_WS=true. Calls reuse one registered voice session, flushing each sentence separately.
Interrupted streams discard their socket so stale audio cannot reach a later turn.
The proxy must relay Text to Dialogue; the legacy TTS WS route is incompatible.
"""

import base64
import json
import logging
import os
import threading
import time

from websockets.exceptions import ConnectionClosed
from typing import Iterator, Optional

from hal.drivers.voice.tts.backend import TTSBackend, TTSRateLimitError
from hal.drivers.voice.tts.elevenlabs import ElevenLabsTTSBackend, _AUDIO_TAG_RE
from hal.drivers.voice.tts.tempo import change_tempo
from hal.drivers.voice.tts.openai import _ensure_openai_v1

logger = logging.getLogger("hal.voice.tts")


class ElevenLabsWSTTSBackend(TTSBackend):
    """ElevenLabs TTS over the stream-input WebSocket. Same output as the HTTP
    backend: raw PCM int16, 24 kHz mono, volume_boost 1.0."""

    supports_synthesis_cancellation = True
    DEFAULT_MODEL = ElevenLabsTTSBackend.DEFAULT_MODEL
    cache_revision = "v4-turbo-dialogue-ws-local-tempo-v1"
    ELEVENLABS_PATH = ElevenLabsTTSBackend.ELEVENLABS_PATH
    # Reuse the HTTP backend's name→voice_id table so saved voices resolve identically.
    VOICE_IDS = ElevenLabsTTSBackend.VOICE_IDS

    def __init__(self, api_key: str, base_url: Optional[str] = None):
        self._turn_lock = threading.Lock()
        self._ws = None
        self._session_key = None
        self._keepalive_stop = None
        self._activity = [time.monotonic(), False]
        self._api_key = api_key
        http_base = _ensure_openai_v1(base_url or "")
        ws_root = (
            http_base.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        )
        route = "/text-to-dialogue/stream-input"
        if not ElevenLabsTTSBackend._is_direct_elevenlabs(http_base):
            route = "/ws/elevenlabs" + route
        self._url_tmpl = os.environ.get("HAL_TTS_ELEVENLABS_WS_URL", "").strip() or (
            ws_root + route + "?model_id={model}&output_format=pcm_24000"
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

    def _disconnect(self):
        if self._keepalive_stop is not None:
            self._keepalive_stop.set()
            self._keepalive_stop = None
        ws, self._ws = self._ws, None
        self._session_key = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def __del__(self):
        self._disconnect()

    def close(self):
        # Teardown may run while recv is blocked; closing wakes the reader.
        self._disconnect()

    def _session(self, voice_id, model):
        key = (voice_id, model)
        if self._ws is not None and self._session_key == key:
            return self._ws
        self._disconnect()
        url = self._url_tmpl.format(voice_id=voice_id, model=model)
        try:
            ws = self._connect(
                url,
                additional_headers={
                    "xi-api-key": self._api_key,
                    "Authorization": f"Bearer {self._api_key}",
                },
                open_timeout=10, close_timeout=1,
            )
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 429:
                raise TTSRateLimitError("ElevenLabs WS rate limit", status_code=429) from exc
            raise
        self._ws, self._session_key = ws, key
        ws.send(json.dumps({"voices": [voice_id], "xi_api_key": self._api_key,
                            "voice_settings": {"speed": 1.0}}))
        stop = threading.Event()
        self._keepalive_stop = stop

        activity = self._activity

        def keepalive():
            # Capture only session objects, not the backend, to avoid retaining it.
            while not stop.wait(10):
                try:
                    if not activity[1] and time.monotonic() - activity[0] >= 60:
                        ws.close()
                        return
                    ws.send(json.dumps({"keep_alive": True}))
                except Exception:
                    return

        threading.Thread(target=keepalive, name="elevenlabs-ws-keepalive", daemon=True).start()
        return ws

    def stream_pcm(self, text: str, voice: str, model: str, speed: float,
                   instructions: Optional[str] = None, cancelled=None) -> Iterator[bytes]:
        if not _AUDIO_TAG_RE.sub("", text or "").strip():
            return
        abandoned = threading.Event()
        stopped = lambda: abandoned.is_set() or (cancelled is not None and cancelled())
        # Head/tail synthesis can overlap. One dialogue socket has one reader;
        # serialize turns so audio cannot be attributed to the wrong sentence.
        while not self._turn_lock.acquire(timeout=0.1):
            if stopped():
                return
        try:
            if stopped():
                return
            self._activity[:] = [time.monotonic(), True]
            voice_id = self.VOICE_IDS.get(voice, voice)
            el_model = model if model.startswith("eleven_") else self.DEFAULT_MODEL
            for attempt in range(2):
                complete = False
                received_audio = False
                try:
                    ws = self._session(voice_id, el_model)
                    ws.send(json.dumps({"inputs": [{"text": text, "voice_id": voice_id}]}))
                    ws.send(json.dumps({"flush": True}))

                    def receive_audio():
                        nonlocal complete, received_audio
                        deadline = time.monotonic() + 30
                        while not stopped():
                            try:
                                raw = ws.recv(timeout=0.2)
                            except TimeoutError:
                                if time.monotonic() >= deadline:
                                    raise TimeoutError("ElevenLabs WS audio timed out")
                                continue
                            msg = json.loads(raw)
                            err = msg.get("error") or msg.get("message")
                            if err:
                                if any(k in str(err).lower() for k in ("quota", "rate limit", "too many", "usage limit")):
                                    raise TTSRateLimitError(f"ElevenLabs WS rate limit: {err}", status_code=429)
                                raise RuntimeError(f"ElevenLabs WS error: {err}")
                            if msg.get("audio"):
                                received_audio = True
                                deadline = time.monotonic() + 30
                                yield base64.b64decode(msg["audio"], validate=True)
                            if msg.get("is_final_audio_for_turn"):
                                complete = True
                                return

                    chunks = receive_audio()
                    output = change_tempo(chunks, speed, 24000, cancelled=stopped)
                    try:
                        yield from output
                    finally:
                        abandoned.set()
                        output.close()
                    return
                except ConnectionClosed:
                    abandoned.clear()
                    # Retry a stale connection once, never replay partial speech.
                    if received_audio or attempt or stopped():
                        raise
                finally:
                    if not complete:
                        self._disconnect()
        finally:
            self._activity[:] = [time.monotonic(), False]
            self._turn_lock.release()
