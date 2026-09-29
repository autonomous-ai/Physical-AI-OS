"""Autonomous STT provider — streaming speech-to-text via Autonomous AI WebSocket API."""

import json
import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode

from hal.drivers.voice.stt.provider import STTProvider, STTSession
from hal.drivers.voice.tts.openai import _ensure_openai_v1

logger = logging.getLogger("hal.voice.stt")
logger.setLevel(logging.INFO)

DEFAULT_MODEL = "flux-general-en"
DEFAULT_LANGUAGE = None

DEFAULT_ENCODING = "linear16"
DEFAULT_ENDPOINTING_MS = 1500
DEFAULT_INTERIM_RESULTS = "true"


def _is_flux(model: str) -> bool:
    return model.startswith("flux")


def _is_nova3(model: str) -> bool:
    return model.startswith("nova-3")


def _keyword_boost_to_terms(keywords: List[str]) -> List[str]:
    """Strip 'word:int' → 'word' for keyterm (nova-3 rejects keywords on upstream Deepgram)."""
    out: List[str] = []
    for k in keywords:
        k = k.strip()
        if not k:
            continue
        out.append(k.split(":", 1)[0].strip())
    return out


def _build_flux_query_params(
    *,
    model: str,
    encoding: str,
    sample_rate: int,
    keywords: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Flux (`flux-*`): model + PCM + channels only (Listen v2 style)."""
    params: Dict[str, Any] = dict(
        model=model,
        encoding=encoding,
        sample_rate=sample_rate,
    )
    terms = _keyword_boost_to_terms(keywords or [])
    if terms:
        params["keyterm"] = terms
        logger.info("Autonomous STT: flux — keyterm=%s", terms)
    return params


def _build_nova_query_params(
    *,
    model: str,
    sample_rate: int,
    channels: int,
    language: str,
    keywords: List[str],
) -> Dict[str, Any]:
    """Nova (`nova-*`): v1-style options; nova-3 uses keyterm, not keywords."""
    params: Dict[str, Any] = dict(
        model=model,
        encoding=DEFAULT_ENCODING,
        sample_rate=sample_rate,
        channels=channels,
        language=language,
        smart_format="true",
        interim_results=DEFAULT_INTERIM_RESULTS,
        endpointing=DEFAULT_ENDPOINTING_MS,
        vad_events="true",
    )
    if not keywords:
        return params
    if _is_nova3(model):
        terms = _keyword_boost_to_terms(keywords)
        if terms:
            params["keyterm"] = terms
            logger.info(
                "Autonomous STT: nova-3 — keyterm=%s (do not use keywords on query)",
                terms,
            )
    else:
        params["keywords"] = ",".join(keywords)
    return params


def _transcriptions_ws_url(ws_base: str, params: Dict[str, Any]) -> str:
    q = urlencode(params, doseq=True)
    return f"{ws_base}/ws/audio/transcriptions?{q}"


class AutonomousSTTSession(STTSession):
    """A single Autonomous AI streaming STT session over WebSocket."""

    def __init__(self, ws_url: str, api_key: str, sample_rate: int):
        self._ws_url = ws_url
        self._api_key = api_key
        self._sample_rate = sample_rate
        self._ws = None
        self._recv_thread: Optional[threading.Thread] = None
        self._closed = threading.Event()
        self._ready = threading.Event()
        self._bytes_sent = 0
        self._logged_first_send = False

    def start(self, on_transcript: Callable[[str, bool], None]) -> bool:
        self._on_transcript_cb = on_transcript
        try:
            from websockets.sync.client import connect
        except ImportError:
            logger.error("websockets package not available")
            self._closed.set()
            return False

        try:
            self._ws = connect(
                self._ws_url,
                additional_headers={"Authorization": f"Token {self._api_key}"},
                open_timeout=10,
                close_timeout=5,
            )
        except Exception as e:
            logger.error("Autonomous STT: WebSocket connect failed: %s", e)
            self._closed.set()
            return False

        logger.info(
            "Autonomous STT: WebSocket OPEN — ready to receive audio (url=%s)",
            self._ws_url[:160] + ("…" if len(self._ws_url) > 160 else ""),
        )
        self._ready.set()

        def recv_loop():
            try:
                for raw in self._ws:
                    if self._closed.is_set():
                        break
                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        r = str(raw)[:200]
                        logger.warning(
                            "Autonomous STT: recv non-JSON (truncated): %s",
                            r + ("…" if len(str(raw)) > 200 else ""),
                        )
                        continue
                    msg_type = msg.get("type", "")

                    if msg_type == "Results":
                        alts = msg.get("channel", {}).get("alternatives", [])
                        if not alts:
                            continue
                        transcript = alts[0].get("transcript", "").strip()
                        if not transcript:
                            continue
                        self._on_transcript_cb(transcript, msg.get("is_final", False))

                    elif msg_type == "TurnInfo":
                        transcript = msg.get("transcript", "").strip()
                        ev = msg.get("event", "")
                        if not transcript:
                            logger.debug("Autonomous STT: TurnInfo — empty transcript (event=%r)", ev)
                            continue
                        self._on_transcript_cb(transcript, ev == "EndOfTurn")
            except Exception as e:
                if not self._closed.is_set():
                    code = getattr(e, "code", None)
                    reason = getattr(e, "reason", None)
                    if code is not None or reason is not None:
                        logger.error(
                            "Autonomous STT: WebSocket closed in recv loop (code=%s reason=%s)",
                            code,
                            reason,
                        )
                    else:
                        logger.error("Autonomous STT: recv loop error: %s", e)
                    if code == 1011 or "1011" in str(e):
                        logger.error(
                            "Autonomous STT: 1011 = server-side failure (often upstream STT). "
                        )
            finally:
                self._closed.set()

        self._recv_thread = threading.Thread(target=recv_loop, daemon=True, name="auto-stt-recv")
        self._recv_thread.start()

        logger.info("Autonomous STT connected — streaming speech (recv thread alive=%s)",
                    self._recv_thread.is_alive())
        return True

    def send_audio(self, data: bytes):
        if self._ws and not self._closed.is_set():
            self._ws.send(data)
            self._bytes_sent += len(data)
            if not self._logged_first_send:
                self._logged_first_send = True
                logger.info(
                    "Autonomous STT: first audio chunk sent to WebSocket (%d bytes, linear16)",
                    len(data),
                )

    def send_keepalive(self):
        """Send a Deepgram KeepAlive so the server doesn't idle-close the WS while we sit
        pre-connected waiting for speech (no audio flowing).
        """
        if self._ws and not self._closed.is_set():
            try:
                self._ws.send(json.dumps({"type": "KeepAlive"}))
            except Exception as e:
                logger.debug("Autonomous STT: keepalive send failed: %s", e)

    def close(self):
        if self._closed.is_set():
            return
        if self._ws:
            try:
                self._ws.send(json.dumps({"type": "CloseStream"}))
                logger.info("Autonomous STT: sent CloseStream — waiting for final transcript")
            except Exception:
                try:
                    self._ws.close()
                except Exception:
                    pass
        if self._recv_thread:
            self._recv_thread.join(timeout=15)
            if self._recv_thread.is_alive():
                logger.warning("Autonomous STT recv thread did not exit in 15s")
                if self._ws:
                    try:
                        self._ws.close()
                    except Exception:
                        pass
        self._closed.set()
        logger.info("Autonomous STT connection closed")

    def is_closed(self) -> bool:
        return self._closed.is_set()


class AutonomousSTT(STTProvider):
    """Autonomous AI streaming STT provider (Deepgram wrapper behind campaign-api)."""

    def __init__(self, api_key: str, base_url: str, sample_rate: int = 16000,
                 channels: int = 1, model: str = DEFAULT_MODEL, language: Optional[str] = None,
                 keywords: Optional[List[str]] = None):
        self._api_key = api_key
        self._sample_rate = sample_rate
        self._channels = channels
        self._model = model
        self._language = language or DEFAULT_LANGUAGE
        self._keywords = keywords or []

        ws_base = _ensure_openai_v1(base_url).replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        if _is_flux(model):
            params = _build_flux_query_params(
                model=model,
                sample_rate=sample_rate,
                encoding=DEFAULT_ENCODING,
                keywords=self._keywords,
            )
        else:
            params = _build_nova_query_params(
                model=model,
                sample_rate=sample_rate,
                channels=channels,
                language=self._language,
                keywords=self._keywords,
            )
        self._ws_url = _transcriptions_ws_url(ws_base, params)
        logger.info("AutonomousSTT ready (url=%s, model=%s)", self._ws_url, model)

    def create_session(self) -> STTSession:
        return AutonomousSTTSession(
            ws_url=self._ws_url,
            api_key=self._api_key,
            sample_rate=self._sample_rate,
        )

    @property
    def available(self) -> bool:
        return self._api_key != ""

    @property
    def name(self) -> str:
        return f"AutonomousSTT({self._model})"
