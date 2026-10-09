"""Deepgram STT provider — streaming speech-to-text via Deepgram WebSocket API."""

import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional

from hal.presets import LANG_EN
from hal.drivers.voice.stt.provider import STTProvider, STTSession

logger = logging.getLogger("hal.voice.stt")

DEFAULT_MODEL = "flux-general-en"
DEFAULT_LANGUAGE = LANG_EN

DEFAULT_INTERIM_RESULTS = "true"
DEFAULT_ENDPOINTING_MS = 1500
DEFAULT_ENCODING = "linear16"


def _is_flux(model: str) -> bool:
    return model.startswith("flux")


def _is_nova3(model: str) -> bool:
    return model.startswith("nova-3")


def _keyword_boost_to_terms(keywords: List[str]) -> List[str]:
    """Strip keyword boost 'word:int' → 'word' for keyterm (nova-3 rejects keywords)."""
    out: List[str] = []
    for k in keywords:
        k = k.strip()
        if not k:
            continue
        out.append(k.split(":", 1)[0].strip())
    return out


def _build_flux_listen_params(
    *,
    model: str,
    sample_rate: int,
) -> Dict[str, Any]:
    """Listen **v2** (Flux): minimal params — no v1-only fields."""
    return dict(
        model=model,
        encoding=DEFAULT_ENCODING,
        sample_rate=sample_rate,
    )


def _build_nova_listen_params(
    *,
    model: str,
    channels: int,
    sample_rate: int,
    language: str,
    keywords: List[str],
) -> Dict[str, Any]:
    """Listen **v1** (nova-2, nova-3, …): keywords or keyterm for nova-3."""
    params: Dict[str, Any] = dict(
        model=model,
        encoding="linear16",
        channels=channels,
        sample_rate=sample_rate,
        language=language,
        smart_format="true",
        interim_results=DEFAULT_INTERIM_RESULTS,
        endpointing=DEFAULT_ENDPOINTING_MS,
        vad_events="true",
    )
    if keywords:
        if _is_nova3(model):
            params["keyterm"] = _keyword_boost_to_terms(keywords)
            logger.info(
                "Deepgram: nova — keyterm=%s (nova-3 does not accept keywords)",
                params["keyterm"],
            )
        else:
            params["keywords"] = keywords
    return params


class DeepgramSession(STTSession):
    """A single Deepgram streaming session (v1 or v2 auto-detected)."""

    def __init__(self, client, keywords: List[str], sample_rate: int, channels: int,
                 language: Optional[str] = None, model: str = DEFAULT_MODEL):
        self._client = client
        self._keywords = keywords
        self._sample_rate = sample_rate
        self._channels = channels
        self._language = language if language else DEFAULT_LANGUAGE
        self._model = model if model else DEFAULT_MODEL
        self._ctx = None
        self._connection = None
        self._listener_thread: Optional[threading.Thread] = None
        self._closed = threading.Event()
        self._listener_ready = threading.Event()
        self._bytes_sent = 0
        self._logged_first_send = False

    def start(self, on_transcript: Callable[[str, bool], None]) -> bool:
        from deepgram.core.events import EventType

        use_flux = _is_flux(self._model)
        api_version = "v2" if use_flux else "v1"

        if use_flux:
            params = _build_flux_listen_params(
                model=self._model,
                sample_rate=self._sample_rate,
            )
        else:
            params = _build_nova_listen_params(
                model=self._model,
                channels=self._channels,
                sample_rate=self._sample_rate,
                language=self._language or DEFAULT_LANGUAGE,
                keywords=self._keywords,
            )

        logger.info(
            "Deepgram: connecting WebSocket (api=%s, model=%s, sample_rate=%s, channels=%s)",
            api_version,
            self._model,
            self._sample_rate,
            self._channels,
        )

        try:
            listener = self._client.listen.v2 if use_flux else self._client.listen.v1
            logger.info("Deepgram: connect params: %s", params)
            self._ctx = listener.connect(**params)
            self._connection = self._ctx.__enter__()
        except Exception as e:
            err = str(e)
            extra = getattr(e, "status_code", None) or getattr(e, "status", None)
            if extra is not None:
                err = f"{err} (http_status={extra})"
            logger.error(
                "Deepgram: WebSocket connect failed (api=%s): %s — "
                "check model/language; nova-3 must use keyterm not keywords",
                api_version,
                err,
            )
            self._closed.set()
            return False

        if use_flux:
            def on_message(message):
                transcript = getattr(message, "transcript", None)
                if not transcript or not transcript.strip():
                    return
                is_final = getattr(message, "is_final", False)
                event = getattr(message, "event", "")
                if event == "EndOfTurn":
                    is_final = True
                on_transcript(transcript.strip(), is_final)
        else:
            from deepgram.listen.v1.types import ListenV1Results

            def on_message(message):
                if not isinstance(message, ListenV1Results):
                    return
                transcript = message.channel.alternatives[0].transcript
                if not transcript or not transcript.strip():
                    return
                on_transcript(transcript.strip(), message.is_final)

        def on_error(error):
            logger.error("Deepgram: WebSocket error: %s", error)
            self._closed.set()

        def on_open(_):
            logger.info(
                "Deepgram: WebSocket OPEN (api=%s, model=%s)",
                api_version,
                self._model,
            )
            self._listener_ready.set()

        def on_close(_):
            logger.info("Deepgram: WebSocket CLOSED")
            self._closed.set()

        conn = self._connection
        conn.on(EventType.OPEN, on_open)
        conn.on(EventType.MESSAGE, on_message)
        conn.on(EventType.ERROR, on_error)
        conn.on(EventType.CLOSE, on_close)

        self._listener_thread = threading.Thread(
            target=conn.start_listening, daemon=True, name="dg-listener",
        )
        self._listener_thread.start()

        if not self._listener_ready.wait(timeout=5):
            logger.error("Deepgram: listener did not become ready within 5s")
            self.close()
            return False

        logger.info("Deepgram: session READY — streaming (api=%s)", api_version)
        return True

    def send_audio(self, data: bytes):
        if self._connection and not self._closed.is_set():
            self._connection.send_media(data)
            self._bytes_sent += len(data)
            if not self._logged_first_send:
                self._logged_first_send = True
                logger.info(
                    "Deepgram: first audio chunk sent to WebSocket (%d bytes, linear16)",
                    len(data),
                )

    def close(self):
        if self._closed.is_set():
            return
        self._closed.set()
        if self._connection:
            try:
                self._connection.send_close_stream()
            except Exception:
                pass
        if self._ctx:
            try:
                self._ctx.__exit__(None, None, None)
            except Exception:
                pass
        if self._listener_thread:
            self._listener_thread.join(timeout=5)
            if self._listener_thread.is_alive():
                logger.error("Deepgram: listener thread did not exit within 5s")
        logger.info("Deepgram: session closed (total audio bytes sent=%s)", self._bytes_sent)

    def is_closed(self) -> bool:
        return self._closed.is_set()


class DeepgramSTT(STTProvider):
    """Deepgram streaming STT provider. Supports nova-2 (v1) and flux (v2)."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, sample_rate: int = 16000,
                 channels: int = 1, language: Optional[str] = None,
                 keywords: Optional[List[str]] = None):
        self._api_key = api_key
        self._model = model
        self._sample_rate = sample_rate
        self._channels = channels
        self._language = language
        self._keywords = keywords or []
        self._client = None
        self._available = False

        try:
            from deepgram import DeepgramClient

            self._client = DeepgramClient(api_key=api_key)
            self._available = True
            api_version = "v2" if _is_flux(model) else "v1"
            logger.info(
                "DeepgramSTT ready (model=%s, %s, lang=%s)",
                model,
                api_version,
                language,
            )
        except ImportError as e:
            logger.error("deepgram-sdk not available — DeepgramSTT disabled (%s)", e)

    def create_session(self, language: Optional[str] = None) -> STTSession:
        model = self._model
        if language and language != self._language:
            # Flux is English-only; another language needs Nova-3.
            model = DEFAULT_MODEL if language == LANG_EN else "nova-3-general"
        return DeepgramSession(
            client=self._client,
            keywords=self._keywords,
            sample_rate=self._sample_rate,
            channels=self._channels,
            language=language or self._language,
            model=model,
        )

    @property
    def available(self) -> bool:
        return self._available and self._api_key != ""
