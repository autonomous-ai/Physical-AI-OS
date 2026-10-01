"""Pipecat v1 provider: an on-device Pipecat pipeline behind the VoiceAgentBase contract (text out).

Invariants: `OutputEvent.gen` bumps per user turn and on interruption, never per response;
`end_turn()` fences the current generation so its late output is dropped by `receive()`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import threading
import time
import uuid
from typing import Any

import numpy as np
from typing_extensions import override

from hal import config as app_config
from hal import cpu_affinity
from hal.drivers.voice.stt.provider import STTProvider
from hal.realtime.config import PipecatV1Config
from hal.realtime.exceptions import PipecatV1Error
from hal.realtime.models import (
    AgentInputEvent,
    AnnounceInput,
    AudioCommitEvent,
    AudioInput,
    FunctionCallOutput,
    FunctionCallResultInput,
    ImageInput,
    InputBase,
    InputEvent,
    InterruptedOutput,
    OutputEvent,
    TextInput,
    TextOutput,
    TurnDoneEvent,
    UserSpeechOutput,
)
from hal.realtime.voice_agent.base import VoiceAgentBase

logger = logging.getLogger(__name__)
usage_logger = logging.getLogger("hal.realtime.usage.pipecat")

# Results of these tools must never run the model again (a follow-up would be spoken on the NEXT turn).
_NO_FOLLOWUP_TOOLS: frozenset[str] = frozenset({"delegate_to_main", "reject_turn"})


def _word_key(word: str) -> str:
    """Punctuation- and case-insensitive word identity ("Lam." == "lam")."""
    return re.sub(r"\W+", "", word).casefold()


def _float32_to_pcm16(audio: np.ndarray) -> bytes:
    clipped = np.clip(audio.astype(np.float32, copy=False), -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16).tobytes()


class PipecatV1Agent(VoiceAgentBase):
    """On-device Pipecat pipeline (device STT + text LLM + tools) as a realtime provider."""

    def __init__(
        self,
        config: PipecatV1Config,
        tools: list[dict[str, Any]] | None = None,
        stt_provider: STTProvider | None = None,
    ) -> None:
        super().__init__(tools=tools)
        self._config: PipecatV1Config = config
        self._stt_provider: STTProvider | None = stt_provider
        self._live: bool = app_config.LIVE_MODE
        # Private asyncio loop hosting the pipeline (thread "pipecat-io").
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._handle: Any = None  # pipecat_pipeline.PipelineHandle
        self._started: threading.Event = threading.Event()
        self._reconnect_backoff: float = config.reconnect_delay_s
        self._reconnect_backoff_max: float = 60.0
        self._last_reconnect_at: float = 0.0
        self._conn_lock: threading.RLock = threading.RLock()
        self._gen: int = 0  # user-turn generation (see module docstring)
        self._fenced_gen: int = -1  # generations end_turn() closed (see end_turn)
        self._user_turn_id: str = ""
        self._user_transcript: str = ""
        # Live mode: only the new part of each cumulative FINAL is emitted; interims never are
        # (a `+=` consumer cannot retract a rewrite).
        self._emitted_transcript: str = ""
        self._manual_turn_open: bool = False  # turn-based mode: frames since last commit
        self._reset_pending: bool = False  # emit output_reset before the next reply
        self._response_open: bool = False
        self._pending_calls: set[str] = set()
        self._followup_expected: bool = False
        self._turn_awaiting: bool = False  # a consumer is waiting on receive()
        self._turn_started_at: float = 0.0
        self._first_text_at: float = 0.0
        # Tool bridge: tool_call_id -> asyncio.Future[(output_json, run_llm)].
        self._tool_futures: dict[str, asyncio.Future] = {}
        self._tool_names: dict[str, str] = {}

    @property
    @override
    def sample_rate(self) -> int:
        return self._config.sample_rate

    @property
    @override
    def supports_announce(self) -> bool:
        """Turn-based only: in live mode the live pump owns the output queue."""
        return not self._live

    @property
    def live(self) -> bool:
        return self._live

    @property
    def llm_busy(self) -> bool:
        """The model is generating or a tool call is in flight."""
        return self._response_open or bool(self._pending_calls)

    def _ensure_stt_provider(self) -> STTProvider:
        if self._stt_provider is not None:
            return self._stt_provider
        from hal.drivers.voice.stt.autonomous import AutonomousSTT

        if not self._config.stt_api_key or not self._config.stt_base_url:
            raise PipecatV1Error("no STT provider: set llm_api_key / llm_base_url or HAL_PIPECAT_STT_*")
        kwargs: dict[str, Any] = {}
        if self._config.stt_model:
            kwargs["model"] = self._config.stt_model
        if self._config.language:
            kwargs["language"] = self._config.language
        self._stt_provider = AutonomousSTT(
            api_key=self._config.stt_api_key,
            base_url=self._config.stt_base_url,
            sample_rate=self._config.sample_rate,
            **kwargs,
        )
        return self._stt_provider

    def _start_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and not self._loop.is_closed():
            return self._loop
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run() -> None:
            cpu_affinity.pin_current_thread(cpu_affinity.FAST)
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()
            loop.close()

        self._loop_thread = threading.Thread(target=_run, daemon=True, name="pipecat-io")
        self._loop_thread.start()
        ready.wait(timeout=5.0)
        self._loop = loop
        return loop

    def _stop_loop(self) -> None:
        loop, thread = self._loop, self._loop_thread
        self._loop, self._loop_thread = None, None
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=self._config.join_timeout_s)

    def _sync_connect(self) -> None:
        try:
            from hal.realtime.voice_agent import pipecat_pipeline
        except ImportError as e:  # pipecat-ai is an optional extra
            raise PipecatV1Error(
                f"pipecat-ai is not installed (uv sync --extra pipecat): {e}"
            ) from e

        stt_provider = self._ensure_stt_provider()
        loop = self._start_loop()
        self._started.clear()
        self._reset_turn_state()
        future = asyncio.run_coroutine_threadsafe(
            pipecat_pipeline.build_and_run(
                self,
                cfg=self._config,
                tools=self._tools,
                stt_provider=stt_provider,
                live=self._live,
            ),
            loop,
        )
        try:
            handle = future.result(timeout=self._config.start_timeout_s)
        except Exception as e:
            raise PipecatV1Error(f"pipeline build failed: {e}") from e
        if not self._started.wait(timeout=self._config.start_timeout_s):
            handle.stop(timeout_s=2.0)
            raise PipecatV1Error("pipeline did not start in time")
        self._handle = handle
        logger.info(
            "[realtime] pipecat_v1 session up: mode=%s llm=%s @ %s stt=%s smart_turn=%s",
            "live" if self._live else "turn",
            self._config.model,
            self._config.base_url,
            stt_provider.name,
            self._config.smart_turn if self._live else "n/a",
        )

    def _sync_disconnect(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.stop(timeout_s=self._config.join_timeout_s)
        self._cancel_tool_futures()
        self._stop_loop()

    def _reset_turn_state(self) -> None:
        self._manual_turn_open = False
        self._reset_pending = False
        self._response_open = False
        self._pending_calls.clear()
        self._followup_expected = False
        self._turn_awaiting = False
        self._user_transcript = ""

    @override
    def _do_connect(self) -> None:
        with self._conn_lock:
            self._sync_connect()

    @override
    def _do_disconnect(self) -> None:
        with self._conn_lock:
            self._sync_disconnect()

    def _ensure_connected(self) -> None:
        if self._stop_event.is_set() or self._connected.is_set():
            return
        now = time.monotonic()
        if now - self._last_reconnect_at < self._reconnect_backoff:
            return
        self._last_reconnect_at = now
        with self._conn_lock:
            if self._stop_event.is_set() or self._connected.is_set():
                return
            try:
                logger.info("[realtime] pipecat_v1 rebuilding pipeline...")
                self._sync_disconnect()
                self._sync_connect()
                self._connected.set()
                self._reconnect_backoff = self._config.reconnect_delay_s
            except Exception as e:  # noqa: BLE001
                self._reconnect_backoff = min(
                    self._reconnect_backoff * 2, self._reconnect_backoff_max
                )
                logger.warning(
                    "[realtime] pipecat_v1 rebuild failed: %s — next retry in ~%.0fs",
                    e, self._reconnect_backoff,
                )

    def _drop_pipeline(self, reason: str) -> None:
        """Mark the session dead; the recv loop rebuilds it (throttled)."""
        if not self._connected.is_set():
            return
        logger.warning("[realtime] pipecat_v1 pipeline lost (%s)", reason)
        self._connected.clear()
        self._fail_fast_turn(reason)

    def _fail_fast_turn(self, reason: str) -> None:
        """End a turn that is waiting on output NOW so it falls back to main."""
        if not self._turn_awaiting:
            return
        self._turn_awaiting = False
        self._recv_queue.put(TurnDoneEvent(user_turn_id=self._user_turn_id))
        logger.info(
            "[realtime] pipecat_v1 %s — ending turn now, falling back to main", reason
        )

    def _sync_send_input(self, inp: InputBase) -> None:
        handle = self._handle
        if handle is None:
            return
        if isinstance(inp, AudioInput):
            if not self._live and not self._manual_turn_open:
                # Turn-based: the first frame opens the user turn (and interrupts a streaming reply).
                self._manual_turn_open = True
                handle.propose_user_turn_start()
                handle.remind_tools()
            handle.queue_audio(_float32_to_pcm16(inp.audio))
        elif isinstance(inp, TextInput):
            handle.append_context(inp.text)
        elif isinstance(inp, AnnounceInput):
            self._start_announcement(handle, inp.text)
        elif isinstance(inp, FunctionCallResultInput):
            self._resolve_tool_call(inp.call_id, inp.output, inp.trigger_response)
        elif isinstance(inp, ImageInput):
            logger.warning("[realtime] pipecat_v1 has no image input — frame dropped")

    def _start_announcement(self, handle: Any, text: str) -> None:
        """Open a device-initiated turn: a new generation (a fenced one would be dropped as stale)."""
        # A committed turn, reply or tool call owns the pipeline: end at once so the caller falls back.
        if self._turn_awaiting or self._response_open or self._pending_calls:
            logger.info("[realtime] pipecat_v1 announcement dropped — pipeline busy")
            self._recv_queue.put(TurnDoneEvent(user_turn_id=self._user_turn_id))
            return
        self._gen += 1
        self._user_turn_id = f"pipecat-announce-{uuid.uuid4().hex[:12]}"
        self._user_transcript = ""
        self._emitted_transcript = ""
        self._reset_pending = False
        self._pending_calls.clear()
        self._followup_expected = False
        self._turn_awaiting = True
        self._turn_started_at = time.monotonic()
        self._first_text_at = 0.0
        handle.run_announcement(text)

    def _sync_commit(self) -> None:
        handle = self._handle
        if handle is None:
            return
        if self._live:
            logger.debug("[realtime] pipecat_v1 commit ignored in live mode")
            return
        self._turn_awaiting = True
        self._turn_started_at = time.monotonic()
        self._first_text_at = 0.0
        if not self._manual_turn_open:
            # Commit with no audio: end the turn now instead of waiting out the receive timeout.
            self._recv_queue.put(TurnDoneEvent(user_turn_id=self._user_turn_id))
            self._turn_awaiting = False
            return
        self._manual_turn_open = False
        handle.propose_user_turn_stop()
        handle.finalize_stt()

    @override
    def end_turn(self) -> None:
        """Fence the current user turn so any follow-up it still produces is dropped (after delegate/reject)."""
        self._newest_output_gen = max(self._newest_output_gen, self._gen + 1)
        self._fenced_gen = max(self._fenced_gen, self._gen)
        self._turn_awaiting = False

    @override
    def _send_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                event: AgentInputEvent = self._send_queue.get(timeout=self._config.queue_poll_s)
            except queue.Empty:
                continue
            if not self._connected.is_set():
                continue  # dropped on the floor, like every provider while down
            try:
                if isinstance(event, AudioCommitEvent):
                    self._sync_commit()
                elif isinstance(event, InputEvent) and event.input is not None:
                    self._sync_send_input(event.input)
            except Exception as e:  # noqa: BLE001
                if self._stop_event.is_set():
                    break
                logger.exception("[realtime] pipecat_v1 send failed: %s", e)
                self._drop_pipeline("send error")

    @override
    def _recv_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._connected.is_set():
                self._ensure_connected()
                if not self._connected.is_set():
                    self._connected.wait(timeout=self._config.queue_poll_s)
                continue
            handle = self._handle
            if handle is not None and not handle.alive:
                self._drop_pipeline("pipeline run ended")
                continue
            self._stop_event.wait(timeout=self._config.queue_poll_s)

    def _begin_tool_call(self, name: str, arguments: str, call_id: str) -> asyncio.Future:
        """Pipeline thread: publish the call, hand back a future for its result."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._tool_futures[call_id] = future
        self._tool_names[call_id] = name
        self.note_server_activity()
        logger.info("[realtime] pipecat_v1 tool call %s(%s)", name, arguments[:200])
        self._recv_queue.put(
            OutputEvent(
                gen=self._gen,
                output=FunctionCallOutput(
                    name=name,
                    arguments=arguments,
                    call_id=call_id,
                    user_transcript=self._user_transcript,
                    user_turn_id=self._user_turn_id,
                ),
            )
        )
        # A call the orchestrator never sees must not hold the LLM forever.
        loop.call_later(
            self._config.tool_result_timeout_s, self._timeout_tool_call, call_id
        )
        return future

    def _timeout_tool_call(self, call_id: str) -> None:
        future = self._tool_futures.pop(call_id, None)
        self._tool_names.pop(call_id, None)
        if future is not None and not future.done():
            logger.warning("[realtime] pipecat_v1 tool %s got no result — answering with an error", call_id)
            future.set_result(('{"error": "no result from the device"}', False))

    def _abandon_tool_call(self, call_id: str) -> None:
        self._tool_futures.pop(call_id, None)
        self._tool_names.pop(call_id, None)

    def _resolve_tool_call(self, call_id: str, output: str, trigger_response: bool) -> None:
        """Send thread: deliver the orchestrator's result to the waiting handler."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        def _deliver() -> None:
            future = self._tool_futures.pop(call_id, None)
            name = self._tool_names.pop(call_id, "")
            if future is None or future.done():
                logger.info("[realtime] pipecat_v1 result for unknown/finished call %s dropped", call_id)
                return
            run_llm = trigger_response and name not in _NO_FOLLOWUP_TOOLS
            future.set_result((output, run_llm))

        loop.call_soon_threadsafe(_deliver)

    def _cancel_tool_futures(self) -> None:
        loop = self._loop
        futures, self._tool_futures = self._tool_futures, {}
        self._tool_names.clear()
        if loop is None or loop.is_closed():
            return
        for future in futures.values():
            loop.call_soon_threadsafe(lambda f=future: f.done() or f.cancel())

    def _ev_started(self) -> None:
        self._started.set()

    def _ev_user_turn_started(self) -> None:
        if self._live and self._handle is not None:
            # Lands before the aggregator appends this turn's transcript.
            self._handle.remind_tools()
        self._gen += 1
        self._user_turn_id = f"pipecat-{uuid.uuid4().hex[:12]}"
        self._user_transcript = ""
        self._emitted_transcript = ""
        self._reset_pending = True
        self._pending_calls.clear()
        self._followup_expected = False
        if self._live:
            self._turn_awaiting = True
            self._turn_started_at = 0.0
            self._first_text_at = 0.0
            self._recv_queue.put(
                OutputEvent(
                    gen=self._gen,
                    output=UserSpeechOutput(
                        turn_id=self._user_turn_id,
                        user_turn_id=self._user_turn_id,
                        method="server_vad",
                    ),
                )
            )

    def _ev_user_turn_stopped(self) -> None:
        if self._live:
            self._turn_started_at = time.monotonic()
            self._recv_queue.put(
                OutputEvent(
                    gen=self._gen,
                    output=UserSpeechOutput(
                        turn_id=self._user_turn_id,
                        user_turn_id=self._user_turn_id,
                        endpoint_at=time.monotonic(),
                        method="server_vad",
                    ),
                )
            )

    def _ev_transcript(self, text: str, is_final: bool) -> None:
        self.note_server_activity()
        if is_final:
            self._user_transcript = (self._user_transcript + " " + text).strip()
        if self._live and self._user_turn_id and is_final:
            chunk = self._transcript_delta(text)
            if chunk or is_final:
                self._recv_queue.put(
                    OutputEvent(
                        gen=self._gen,
                        output=UserSpeechOutput(
                            turn_id=self._user_turn_id,
                            user_turn_id=self._user_turn_id,
                            transcript=chunk,
                            transcript_finished=is_final,
                            method="provider_transcript",
                        ),
                    )
                )
        if is_final:
            logger.info("[realtime] pipecat_v1 <<< user said: %r", text[:200])

    def _transcript_delta(self, text: str) -> str:
        """The part of a cumulative STT final not yet emitted for this turn.

        A rewrite yields the words after the longest common word prefix; a shorter one yields nothing.
        """
        emitted = self._emitted_transcript
        if not emitted:
            self._emitted_transcript = text
            return text
        if text.startswith(emitted):
            chunk = text[len(emitted):]
            self._emitted_transcript = text
            return chunk
        if emitted.startswith(text):
            return ""
        old_words, new_words = emitted.split(), text.split()
        common = 0
        while common < min(len(old_words), len(new_words)) and _word_key(old_words[common]) == _word_key(new_words[common]):
            common += 1
        chunk = " ".join(new_words[common:])
        self._emitted_transcript = text
        return (" " + chunk) if chunk else ""

    def _ev_stt_turn_finalized(self, had_text: bool) -> None:
        if had_text or self._live:
            return
        # Nothing transcribed: end the turn so HAL falls back with its own transcript.
        if self._turn_awaiting:
            self._turn_awaiting = False
            logger.info("[realtime] pipecat_v1 empty transcript — ending turn")
            self._recv_queue.put(TurnDoneEvent(user_turn_id=self._user_turn_id))

    def _ev_response_started(self) -> None:
        self.note_server_activity()
        self._response_open = True
        if self._reset_pending:
            self._reset_pending = False
            self._recv_queue.put(
                OutputEvent(
                    gen=self._gen,
                    output=InterruptedOutput(reason="output_reset", user_turn_id=self._user_turn_id),
                )
            )

    def _ev_text(self, text: str) -> None:
        self.note_server_activity()
        if not text:
            return
        if not self._first_text_at and self._turn_started_at:
            self._first_text_at = time.monotonic()
            usage_logger.info(
                "[realtime] pipecat_v1 first text +%.2fs after turn end",
                self._first_text_at - self._turn_started_at,
            )
        self._recv_queue.put(
            OutputEvent(gen=self._gen, output=TextOutput(text=text, user_turn_id=self._user_turn_id))
        )

    def _ev_calls_started(self, call_ids: list[str]) -> None:
        self.note_server_activity()
        self._pending_calls.update(call_ids)
        self._followup_expected = False

    def _ev_call_result(self, call_id: str, run_llm: bool | None) -> None:
        self.note_server_activity()
        self._pending_calls.discard(call_id)
        if run_llm is not False:
            self._followup_expected = True
        if self._pending_calls:
            return
        if self._followup_expected:
            return  # the follow-up response's end closes the turn
        if not self._response_open:
            self._emit_turn_done(completed=True)

    def _ev_response_ended(self) -> None:
        self.note_server_activity()
        if not self._response_open:
            return  # already closed by an error or an interruption
        self._response_open = False
        if self._pending_calls:
            return  # tool results decide (see _ev_call_result)
        self._emit_turn_done(completed=True)

    def _ev_interruption(self) -> None:
        was_busy = self._response_open or bool(self._pending_calls)
        self._response_open = False
        self._pending_calls.clear()
        self._followup_expected = False
        if not was_busy:
            return
        self._gen += 1
        if self._live:
            self._recv_queue.put(
                OutputEvent(
                    gen=self._gen,
                    output=InterruptedOutput(
                        reason="server_interrupt", at=time.monotonic(), user_turn_id=self._user_turn_id
                    ),
                )
            )
            logger.info("[realtime] pipecat_v1 reply interrupted by the user (gen=%d)", self._gen)
            self._emit_turn_done(completed=False)
        # Turn-based: a TurnDone on interruption would end the NEW turn empty.

    def _emit_turn_done(self, *, completed: bool) -> None:
        self._turn_awaiting = False
        if self._gen <= self._fenced_gen:
            # The consumer already left this turn; a late terminal would end the next turn empty.
            return
        self._recv_queue.put(
            TurnDoneEvent(execution_completed=completed, user_turn_id=self._user_turn_id)
        )

    def _ev_error(self, error: str, fatal: bool) -> None:
        logger.error("[realtime] pipecat_v1 pipeline error (fatal=%s): %s", fatal, error[:300])
        if fatal:
            self._drop_pipeline("fatal pipeline error")
        elif self._response_open or self._pending_calls:
            # Unblock the consumer now (execution_completed=False -> main-agent fallback).
            self._response_open = False
            self._pending_calls.clear()
            self._emit_turn_done(completed=False)

    def _ev_ttfb(self, processor: str, value: float) -> None:
        usage_logger.info("[realtime] pipecat_v1 ttfb %s %.3fs", processor, value)

    def _ev_llm_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        usage_logger.info(
            "[realtime] pipecat_v1 llm usage model=%s in=%d out=%d",
            self._config.model, prompt_tokens, completion_tokens,
        )


__all__ = ["PipecatV1Agent", "PipecatV1Error"]
