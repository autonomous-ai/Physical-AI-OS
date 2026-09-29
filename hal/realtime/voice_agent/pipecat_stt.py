"""Pipecat STT stage backed by HAL's own streaming STT provider.

per_turn=True: one provider session per turn, closed on finalize; per_turn=False: one long session.
All provider I/O runs on one sender thread to keep the asyncio loop unblocked and ordered.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from collections.abc import AsyncGenerator, Callable
from typing import Any

from dataclasses import dataclass

from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    SystemFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.stt_service import STTService
from pipecat.utils.time import time_now_iso8601

from hal.drivers.voice.stt.provider import STTProvider, STTSession

logger = logging.getLogger(__name__)

# Sender-thread ops. Audio is bytes; the sentinels close / open the session.
_OP_FINALIZE = object()
_OP_STOP = object()

# The relay idle-closes a silent socket.
_KEEPALIVE_S: float = 5.0
_OPEN_RETRY_S: float = 2.0


@dataclass
class STTFinalizeFrame(SystemFrame):
    """Turn-based mode: the utterance is committed, flush and close the session.

    Sent through the pipeline behind the audio so it can't race the last frames.
    """


class HALSTTService(STTService):
    """Pipecat `STTService` that streams audio through a HAL `STTProvider`."""

    def __init__(
        self,
        provider: STTProvider,
        *,
        per_turn: bool,
        sample_rate: int = 16000,
        on_transcript: Callable[[str, bool], None] | None = None,
        on_turn_finalized: Callable[[bool], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._provider = provider
        self._per_turn = per_turn
        self._on_transcript = on_transcript
        self._on_turn_finalized = on_turn_finalized
        self._ops: queue.Queue[Any] = queue.Queue()
        self._sender: threading.Thread | None = None
        self._session: STTSession | None = None
        self._turn_had_text: bool = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._last_send_at: float = 0.0
        # A connect blocks up to 10 s; don't retry on every 64 ms frame.
        self._next_open_at: float = 0.0

    async def start(self, frame):  # type: ignore[override]
        await super().start(frame)
        self._loop = asyncio.get_running_loop()
        if self._sender is None or not self._sender.is_alive():
            self._sender = threading.Thread(
                target=self._sender_loop, daemon=True, name="pipecat-stt"
            )
            self._sender.start()

    async def stop(self, frame):  # type: ignore[override]
        await super().stop(frame)
        self._shutdown()

    async def cancel(self, frame):  # type: ignore[override]
        await super().cancel(frame)
        self._shutdown()

    def _shutdown(self) -> None:
        if self._sender is not None and self._sender.is_alive():
            self._ops.put(_OP_STOP)

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Hand one PCM16 chunk to the sender thread (never blocks the loop)."""
        self._ops.put(audio)
        yield None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, STTFinalizeFrame):
            await super().process_frame(frame, direction)
            self._ops.put(_OP_FINALIZE)
            return
        await super().process_frame(frame, direction)

    def _sender_loop(self) -> None:
        while True:
            try:
                op = self._ops.get(timeout=1.0)
            except queue.Empty:
                self._maybe_keepalive()
                continue
            if op is _OP_STOP:
                self._close_session()
                return
            if op is _OP_FINALIZE:
                # close() blocks until the final transcript is flushed; read the flag after it.
                self._close_session()
                with self._lock:
                    had_text = self._turn_had_text
                    self._turn_had_text = False
                if self._on_turn_finalized is not None:
                    self._on_turn_finalized(had_text)
                continue
            self._send_audio(op)

    def _maybe_keepalive(self) -> None:
        session = self._session
        if session is None or session.is_closed():
            return
        if time.monotonic() - self._last_send_at < _KEEPALIVE_S:
            return
        keepalive = getattr(session, "send_keepalive", None)
        if callable(keepalive):
            keepalive()
        self._last_send_at = time.monotonic()

    def _open_session(self) -> bool:
        if time.monotonic() < self._next_open_at:
            return False
        session = self._provider.create_session()
        if not session.start(self._on_provider_transcript):
            self._next_open_at = time.monotonic() + _OPEN_RETRY_S
            logger.warning("[pipecat-stt] STT session failed to open (%s)", self._provider.name)
            return False
        self._next_open_at = 0.0
        with self._lock:
            self._session = session
            self._turn_had_text = False
        self._last_send_at = time.monotonic()
        logger.info("[pipecat-stt] session open (%s, per_turn=%s)", self._provider.name, self._per_turn)
        return True

    def _close_session(self) -> None:
        with self._lock:
            session = self._session
            self._session = None
        if session is None:
            return
        try:
            session.close()  # blocks until the server flushed the final transcript
        except Exception as e:  # noqa: BLE001 — a dead socket must not kill the sender
            logger.debug("[pipecat-stt] close error: %s", e)

    def _send_audio(self, audio: bytes, *, retry: bool = True) -> None:
        session = self._session
        if session is None or session.is_closed():
            if session is not None and not self._per_turn:
                logger.info("[pipecat-stt] live session dropped — reopening")
            if not self._open_session():
                return
            session = self._session
        try:
            session.send_audio(audio)  # type: ignore[union-attr]
            self._last_send_at = time.monotonic()
        except Exception as e:  # noqa: BLE001
            # The relay idle-closes live sessions between calls; reopen and resend the frame once.
            logger.warning("[pipecat-stt] send failed: %s — %s", e, "reopening" if retry else "dropping session")
            self._close_session()
            if retry:
                self._send_audio(audio, retry=False)

    def _on_provider_transcript(self, text: str, is_final: bool) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._turn_had_text = True
        if self._on_transcript is not None:
            try:
                self._on_transcript(text, is_final)
            except Exception:  # noqa: BLE001
                logger.exception("[pipecat-stt] on_transcript callback failed")
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        frame: Frame
        if is_final:
            frame = TranscriptionFrame(
                text=text, user_id="", timestamp=time_now_iso8601(), finalized=True
            )
        else:
            frame = InterimTranscriptionFrame(text=text, user_id="", timestamp=time_now_iso8601())
        loop.call_soon_threadsafe(self._schedule_push, frame)

    def _schedule_push(self, frame: Frame) -> None:
        # Let the processor's task manager own the push so teardown can cancel it.
        self.create_task(self.push_frame(frame))
