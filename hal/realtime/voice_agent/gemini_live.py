"""Gemini Live voice agent implementation — queue-based threading."""

import asyncio
import base64
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any, override
from uuid import uuid4

import cv2
import google.genai as genai
import numpy as np
import numpy.typing as npt
from google.genai import errors as genai_errors
from google.genai import types
from google.genai.live import AsyncSession
from websockets.exceptions import ConnectionClosed

from hal import config as app_config
from hal.realtime.config import GeminiConfig, gemini_needs_idle_workaround
from hal.realtime.voice_agent.gemini_status import install_interaction_status
from hal.realtime.enums import GeminiThinkingLevel
from hal.realtime.models import (
    AgentInputEvent,
    AnnounceInput,
    AudioCommitEvent,
    AudioStreamEndEvent,
    AudioInput,
    AudioOutput,
    FunctionCallOutput,
    FunctionCallResultInput,
    ImageInput,
    InputBase,
    InputEvent,
    InterruptedOutput,
    UserSpeechOutput,
    ExecutionOutput,
    OutputEvent,
    TextInput,
    TextOutput,
    TurnDoneEvent,
)
from hal.realtime.utils import float32_to_pcm16_bytes, pcm16_bytes_to_float32
from hal.realtime.voice_agent.base import (
    AudioTurnSessionChanged,
    BoundAudioCommitEvent,
    BoundAudioInputEvent,
    VoiceAgentBase,
)

logger = logging.getLogger(__name__)
# Per-turn token/cost lines go to their own file (gemini_usage.log) via a
# dedicated logger configured in server_support/log_setup.py (propagate=False),
# so they don't mix into server.log.
usage_logger = logging.getLogger("hal.realtime.usage")

# Gemini Live pricing, USD per 1M tokens, keyed (direction, modality), PER MODEL.
# Source: ai.google.dev/gemini-api/docs/pricing (verified 2026-06-29). Audio rates
# happen to match across these models ($3 in / $12 out) — only text differs — but
# the table is explicit per model so adding a new model is one entry and a wrong
# audio rate can't hide behind a shared default. Keys match as a SUBSTRING of
# self._config.model so a dated preview suffix (…-preview-12-2025) still resolves.
_GEMINI_RATES: dict[str, dict[tuple[str, str], float]] = {
    "gemini-2.5-flash-native-audio": {
        ("in", "TEXT"): 0.50, ("in", "AUDIO"): 3.0,
        ("out", "TEXT"): 2.0, ("out", "AUDIO"): 12.0,
    },
    "gemini-3.1-flash-live": {
        ("in", "TEXT"): 0.75, ("in", "AUDIO"): 3.0,
        ("out", "TEXT"): 4.5, ("out", "AUDIO"): 12.0,
    },
    # Promo rates through 2026-12-31 (ai.google.dev pricing, verified 2026-09-17);
    # Google announced they double afterwards. Covers both `gemini-3.8-live` and
    # `gemini-3.8-live-extended-thinking` (substring match).
    "gemini-3.8-live": {
        ("in", "TEXT"): 0.75, ("in", "AUDIO"): 3.0,
        ("out", "TEXT"): 4.5, ("out", "AUDIO"): 12.0,
    },
}
# Unknown model → fall back to the entry with the highest text-out rate (the
# dominant text cost), so a future/untabled model logs a cost CEILING rather than
# an under-report. Avoids the old `"native-audio" in model` heuristic mis-pricing
# a hypothetical 3.x native-audio model at 2.5 rates.
_GEMINI_RATES_FALLBACK: dict[tuple[str, str], float] = max(
    _GEMINI_RATES.values(), key=lambda r: r[("out", "TEXT")]
)


def _gemini_rates_for(model: str) -> dict[tuple[str, str], float]:
    """Resolve the per-1M-token rate table for a model name (substring match);
    unknown models fall back to the most expensive table (cost = ceiling)."""
    for key, table in _GEMINI_RATES.items():
        if key in model:
            return table
    return _GEMINI_RATES_FALLBACK


class GeminiLiveAgent(VoiceAgentBase):
    def __init__(
        self,
        config: GeminiConfig,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(tools=tools)
        self._config: GeminiConfig = config
        client_kwargs: dict = {"api_key": config.api_key}
        if config.base_url:
            client_kwargs["http_options"] = types.HttpOptions(base_url=config.base_url)
        self._client: genai.Client = genai.Client(**client_kwargs)
        # Keep the Live WS alive across IDLE gaps — ONLY for native-audio (the
        # model with the idle-resume bug; see gemini_needs_idle_workaround). The
        # browser raw-WS client survives long silence on this same proxy, so the
        # idle death is client-side. DISABLING pings entirely (ping_interval=None)
        # was WRONG: with no outbound traffic the NAT/proxy path drops the idle TCP
        # (WS 1006) and the next spoken turn lands on a dead session → WS 1011.
        # Instead KEEP pinging every 20s (outbound traffic holds the path open like
        # the browser's TCP keepalive) but ping_timeout=None so a missing pong (the
        # proxy doesn't pong) never self-closes. 3.1 doesn't need this → leave the
        # SDK defaults. google-genai spreads this dict into websockets.connect(...).
        if gemini_needs_idle_workaround(config.model):
            try:
                self._client._api_client._websocket_ssl_ctx["ping_interval"] = 20
                self._client._api_client._websocket_ssl_ctx["ping_timeout"] = None
            except Exception as e:  # pragma: no cover - private SDK field, best effort
                logger.warning("[realtime] could not set Gemini WS keepalive: %s", e)
        self._session: AsyncSession | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._io_thread: threading.Thread | None = None
        self._resumption_handle: str | None = None
        # Per-turn timing markers. These intentionally use monotonic time so
        # NTP adjustments cannot skew latency diagnostics.
        self._last_audio_sent_at: float | None = None
        self._activity_end_sent_at: float | None = None
        self._first_audio_received: bool = False
        self._vad_disabled: bool = not config.vad_enabled
        self._turn_gen: int = 0
        self._activity_started: bool = False
        # Gemini rejects send_realtime_input while a tool call it emitted is still
        # unanswered, and closes the session with 1008 ("The operation was
        # aborted") — a deliberate policy close, not a transport drop. Track the
        # call_ids currently in flight and drop all normal client input while the
        # set is non-empty. A fire-and-forget tool (trigger_response=False) is
        # deliberately not acknowledged to Gemini, so that session must be
        # replaced before another turn rather than locally clearing the call.
        self._pending_tool_calls: set[str] = set()
        self._pending_tool_names: dict[str, str] = {}
        self._cancelled_look_calls: set[str] = set()
        self.supports_look_continuation = False
        self._pending_image = None
        self._requires_fresh_session: bool = False
        self._reject_followup_barrier: bool = False
        self._user_transcript: str = ""
        self._gated_audio_frames: int = 0
        self._reconnect_delay_s: float = config.reconnect_delay_s
        self._last_reconnect_at: float = 0.0
        # Exponential backoff: the recv loop self-reconnects when disconnected, so a
        # persistent failure (e.g. Gemini usage-limit 4029) would otherwise hammer
        # the endpoint every reconnect_delay_s. Grow the throttle on each failed
        # reconnect (capped), reset to the base on success.
        self._reconnect_backoff: float = config.reconnect_delay_s
        self._reconnect_backoff_max: float = 60.0
        self._max_retries: int = config.max_retries
        self._send_timeout_s: float = config.send_timeout_s
        self._recv_timeout_s: float = config.recv_timeout_s
        self._queue_poll_s: float = config.queue_poll_s
        self._join_timeout_s: float = config.join_timeout_s
        # Signals that the model is idle (no active turn). Set by default,
        # cleared when activityEnd is sent, set again on turn_complete.
        self._turn_done: threading.Event = threading.Event()
        self._turn_done.set()

    @property
    @override
    def sample_rate(self) -> int:
        return self._config.sample_rate

    @property
    def output_sample_rate(self) -> int:
        # Gemini Live always streams native audio output at 24 kHz, regardless of
        # the 16 kHz input rate (`sample_rate`).
        return 24000

    @property
    def audio_session(self) -> object | None:
        return self._session

    @property
    @override
    def requires_fresh_session(self) -> bool:
        """Whether unresolved tools or a handoff require a new session."""
        return getattr(self, "_requires_fresh_session", False) or bool(
            self._pending_tool_calls
        )

    @property
    @override
    def supports_announce(self) -> bool:
        """Turn-based sessions on models that accept text turns.

        2.5 native-audio is excluded for the same reason send_text drops text
        there (wire-shape guard, WS 1011). Live mode is excluded because the
        open uplink and the live pump own the session and its output queue.
        """
        return not app_config.LIVE_MODE and not gemini_needs_idle_workaround()

    def _activity_detection(self) -> types.AutomaticActivityDetection:
        """Provider-side VAD settings for this session.

        Only the `disabled` flag matters on the turn-based path, which brackets
        turns itself. In live mode the rest decide whether OUR OWN ECHO opens a
        turn: at the 4-10 dB ERLE this hardware achieves, the residual reaching
        the uplink is quieter but still speech-shaped, and a VAD keys on speech
        presence rather than level. Left at the API defaults the model
        interrupted itself on almost every reply (device-observed 2026-09-08 on
        lamp-ee17). A LOW start sensitivity plus a prefix-padding floor makes an
        onset need more evidence than a short echo burst carries.

        Every field is omitted when unset, so the provider's own default stands
        rather than us guessing a value for it.
        """
        kwargs: dict[str, Any] = {"disabled": self._vad_disabled}
        if self._vad_disabled:
            return types.AutomaticActivityDetection(**kwargs)
        starts = {
            "high": types.StartSensitivity.START_SENSITIVITY_HIGH,
            "low": types.StartSensitivity.START_SENSITIVITY_LOW,
        }
        ends = {
            "high": types.EndSensitivity.END_SENSITIVITY_HIGH,
            "low": types.EndSensitivity.END_SENSITIVITY_LOW,
        }
        if self._config.vad_start_sensitivity in starts:
            kwargs["start_of_speech_sensitivity"] = starts[
                self._config.vad_start_sensitivity
            ]
        if self._config.vad_end_sensitivity in ends:
            kwargs["end_of_speech_sensitivity"] = ends[
                self._config.vad_end_sensitivity
            ]
        if self._config.vad_prefix_padding_ms > 0:
            kwargs["prefix_padding_ms"] = self._config.vad_prefix_padding_ms
        if self._config.vad_silence_ms > 0:
            kwargs["silence_duration_ms"] = self._config.vad_silence_ms
        logger.info(
            "[realtime] server VAD: start=%s end=%s prefix_padding=%sms silence=%sms",
            self._config.vad_start_sensitivity or "(provider default)",
            self._config.vad_end_sensitivity or "(provider default)",
            self._config.vad_prefix_padding_ms or "(provider default)",
            self._config.vad_silence_ms or "(provider default)",
        )
        return types.AutomaticActivityDetection(**kwargs)

    def _build_config(self) -> types.LiveConnectConfig:
        lang: str | None = self._config.language
        lang_codes: list[str] | None = (
            ["vi-VN" if lang == "vi" else lang]
            if lang and self._config.use_language_codes else None
        )

        logger.info("[realtime] input transcription language hints: %s", lang_codes or "auto")

        if "2.5" in self._config.model or "native-audio" in self._config.model:
            budget = 0 if self._config.thinking_level == GeminiThinkingLevel.MINIMAL else -1
            thinking_config = types.ThinkingConfig(
                thinking_budget=budget, include_thoughts=False
            )
        elif "3.8-live" in self._config.model and "extended-thinking" not in self._config.model:
            # `gemini-3.8-live` (no reasoning) rejects thinkingLevel outright:
            # "thinkingLevel is not supported (omit from setup)".
            thinking_config = None
        else:
            level = self._config.thinking_level
            if "extended-thinking" in self._config.model and level == GeminiThinkingLevel.MINIMAL:
                # 3.8 extended-thinking accepts LOW/MEDIUM/HIGH only; a MINIMAL
                # carried over from a 3.1 config.json would fail the setup.
                logger.info("[realtime] thinking_level MINIMAL unsupported on %s — using LOW", self._config.model)
                level = GeminiThinkingLevel.LOW
            thinking_config = types.ThinkingConfig(
                thinking_level=level.value, include_thoughts=False
            )

        live_config: types.LiveConnectConfig = types.LiveConnectConfig(
            # AUDIO always. TEXT-only was tried on device 2026-09-08 and the
            # model REFUSES it: WS 1007 "The requested combination of response
            # modalities (TEXT) is not supported by the model.
            # models/gemini-3.1-flash-live-preview". So when our own TTS speaks
            # the reply (REALTIME_NATIVE_AUDIO=false) the generated audio is
            # received and DISCARDED by the consumer — billed but unused. There
            # is no cheaper wire shape available on this model.
            response_modalities=[types.Modality.AUDIO],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self._config.voice.value,
                    )
                ),
                language_code=(
                    None if "native-audio" in self._config.model else lang
                ),
            ),
            system_instruction=self._config.instructions,
            input_audio_transcription=types.AudioTranscriptionConfig(
                language_hints=(
                    types.LanguageHints(language_codes=lang_codes) if lang_codes else None
                ),
            ),
            # Always on: with AUDIO the only modality, this transcript is the
            # ONLY way to get the reply as text, which is what our TTS speaks
            # when native audio is off.
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=self._activity_detection(),
            ),
            thinking_config=thinking_config,
        )

        live_tools: list[types.Tool] = []
        if self._tools:
            # extended-thinking accepts ONLY NON_BLOCKING tool declarations;
            # a BLOCKING one makes it error mid-turn and speak a canned
            # "I'm sorry, an error occurred." after every tool call. Other live
            # models take BLOCKING (the default), so leave behavior unset there.
            behavior = (
                types.Behavior.NON_BLOCKING
                if "extended-thinking" in self._config.model
                else None
            )
            declarations: list[types.FunctionDeclaration] = [
                types.FunctionDeclaration(
                    name=tool["name"],
                    description=tool.get("description", ""),
                    parameters=tool.get("parameters"),
                    behavior=behavior,
                )
                for tool in self._tools
            ]
            if behavior == types.Behavior.NON_BLOCKING:
                declarations.append(types.FunctionDeclaration(
                    name="complete_response",
                    description=(
                        "Confirm a finished direct answer ONLY for a greeting, general knowledge, "
                        "a completed public lookup, or a visual question answered from look. "
                        "Never use for an action, music playback, stored memory or past conversations, "
                        "account access, Harness/code work, a promise/filler, an error or unresolved work. "
                        "Those require delegate_to_main, even if you already said you would help. "
                        "Call only AFTER delivering the actual answer; a receipt such as "
                        "'Let me check our conversation history' is NOT an answer."
                    ),
                    parameters={"type": "object", "properties": {}},
                    behavior=types.Behavior.NON_BLOCKING,
                ))
            live_tools.append(types.Tool(function_declarations=declarations))

        if self._config.google_search_enabled:
            live_tools.append(types.Tool(google_search=types.GoogleSearch()))

        if live_tools:
            live_config.tools = live_tools

        if self._config.session_resumption_enabled:
            live_config.session_resumption = types.SessionResumptionConfig(
                handle=self._resumption_handle,
            )

        return live_config

    # --- Async internals (run on private event loop) ---

    async def _async_connect(self) -> None:
        # A reopened session must not inherit input ownership or playback acks.
        self.supports_look_continuation = False
        self._reject_followup_barrier = False
        self._live_user_turn_id = ""
        self._live_speech_emitted = False
        self._awaiting_playback_turn_complete = False
        logger.info(
            "Connecting to Gemini Live API (base_url=%s, model=%s)",
            self._config.base_url,
            self._config.model,
        )
        self._exit_stack = AsyncExitStack()
        self._session = await self._exit_stack.enter_async_context(
            self._client.aio.live.connect(
                model=self._config.model,
                config=self._build_config(),
            )
        )
        install_interaction_status(self._session)
        logger.info("[realtime] Gemini Live session open (voice=%s)", self._config.voice)

    async def _async_disconnect(self) -> None:
        exit_stack = self._exit_stack
        # Clear shared state before awaiting provider cleanup. A close can stall
        # on a bad proxy, but no sender/receiver should see its stale session.
        self._exit_stack = None
        self._session = None
        if exit_stack is not None:
            logger.info("[realtime] Disconnecting from Gemini Live API")
            await exit_stack.aclose()

    @staticmethod
    def _run_io_loop(loop: asyncio.AbstractEventLoop) -> None:
        """Run and own a Gemini IO loop until teardown cancels its tasks.

        The owning thread closes the loop only after all pending coroutines have
        been cancelled. Closing it from the rebuild thread while a receive()
        coroutine is still pending leaves a live ``gemini-io`` thread behind.
        """
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            # Even with no pending tasks, a completed receive may still have
            # its run_coroutine_threadsafe completion callback queued. Pump
            # the loop before closing so the waiting thread receives its
            # result/error instead of leaking an unretrieved task exception.
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
            loop.close()
            asyncio.set_event_loop(None)

    def _stop_io_loop(self) -> None:
        """Cancel the IO loop's tasks and wait briefly for its owner to exit."""
        loop = self._loop
        io_thread = self._io_thread
        self._loop = None
        self._io_thread = None
        if loop is None:
            return

        def _cancel_tasks_and_stop() -> None:
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.stop()

        try:
            loop.call_soon_threadsafe(_cancel_tasks_and_stop)
        except RuntimeError:
            # The loop already stopped/closed; its owner is already unwinding.
            pass
        if io_thread is not None and io_thread is not threading.current_thread():
            io_thread.join(timeout=self._join_timeout_s)
            if io_thread.is_alive():
                logger.warning("[realtime] Gemini IO thread did not stop within %.1fs", self._join_timeout_s)

    def _tool_call_pending(self) -> bool:
        """True while Gemini is waiting on a tool result and rejects input."""
        return bool(self._pending_tool_calls)

    def _invalidate_look_images(self, call_ids) -> None:
        """Prevent an in-flight capture from answering a cancelled question."""
        if not hasattr(self, "_cancelled_look_calls"):
            self._cancelled_look_calls = set()
        names = getattr(self, "_pending_tool_names", {})
        self._cancelled_look_calls.update(cid for cid in call_ids if names.get(cid) == "look")

    def _clear_pending_tool_calls(self) -> None:
        self._pending_tool_calls.clear()
        getattr(self, "_pending_tool_names", {}).clear()
        getattr(self, "_cancelled_look_calls", set()).clear()
        self._pending_image = None
        if self._gated_audio_frames:
            logger.debug(
                "[realtime] Dropped %d audio frame(s) during tool call",
                self._gated_audio_frames,
            )
            self._gated_audio_frames = 0

    async def _async_send_input(
        self, _input: InputBase | None, *, session: object | None = None,
    ) -> None:
        if session is not None:
            self.validate_audio_session(session)
        if self._session is None or _input is None:
            return
        audio_session = self._session if session is None else session

        # if isinstance(_input, AudioInput):
        #     detail = f"{len(_input.audio)} samples"
        # elif isinstance(_input, ImageInput):
        #     detail = f"{_input.image.shape}"
        # elif isinstance(_input, TextInput):
        #     detail = f"{len(_input.text)} chars"
        # elif isinstance(_input, FunctionCallResultInput):
        #     detail = f"call_id={_input.call_id}"
        # else:
        #     detail = "unknown input type"

        # logger.info("Sending %s to Gemini Live: %s", type(_input).__name__, detail)

        if isinstance(_input, AnnounceInput) and (
            self._tool_call_pending() or self._activity_started
        ):
            # A tool call or an open user activity owns the session: sending a
            # text turn now would be rejected (1008) or split the user's turn.
            # End the announcement at once so the caller falls back instead of
            # waiting out the receive timeout.
            logger.info(
                "[realtime] Announcement dropped (tool pending=%s, activity open=%s)",
                self._tool_call_pending(), self._activity_started,
            )
            self._recv_queue.put(TurnDoneEvent())
            return
        if not isinstance(_input, FunctionCallResultInput) and self._tool_call_pending():
            if isinstance(_input, AudioInput):
                self._gated_audio_frames += 1
            elif isinstance(_input, ImageInput) and getattr(self, "supports_look_continuation", False):
                # A sibling async tool can still be pending after look's ACK.
                # Keep one current frame until all tool responses reach the wire.
                self._pending_image = _input
                logger.info("[realtime] Holding look frame until pending tool acknowledgements finish")
            else:
                logger.debug(
                    "[realtime] Dropped %s while tool call(s) %s are pending",
                    type(_input).__name__,
                    sorted(self._pending_tool_calls),
                )
            return
        if isinstance(_input, AudioInput):
            # When VAD is disabled, send activityStart before first audio
            if self._vad_disabled and not self._activity_started:
                await audio_session.send_realtime_input(
                    activity_start=types.ActivityStart()
                )
                if session is not None:
                    self.validate_audio_session(session)
                self._activity_started = True
                logger.debug("[realtime] Sent activityStart (manual VAD)")

            pcm_bytes: bytes = float32_to_pcm16_bytes(_input.audio)
            await audio_session.send_realtime_input(
                audio=types.Blob(
                    data=pcm_bytes,
                    mime_type=f"audio/pcm;rate={self._config.sample_rate}",
                )
            )
            if session is not None:
                self.validate_audio_session(session)
            # This is updated per frame; once AudioCommitEvent is processed it
            # represents the final frame that reached the provider.
            self._last_audio_sent_at = time.monotonic()
        elif isinstance(_input, TextInput):
            await self._session.send_client_content(
                turns=types.Content(
                    parts=[types.Part(text=_input.text)],
                    role="user",
                ),
                turn_complete=False,
            )
        elif isinstance(_input, AnnounceInput):
            # A complete text turn: the model answers it like a user turn. The
            # next commit waits on _turn_done, so it cannot interleave with
            # this response; turn_complete sets it again.
            self._turn_done.clear()
            await self._session.send_client_content(
                turns=types.Content(
                    parts=[types.Part(text=_input.text)],
                    role="user",
                ),
                turn_complete=True,
            )
        elif isinstance(_input, ImageInput):
            _: bool
            buf: npt.NDArray[np.uint8]
            _, buf = cv2.imencode(".jpg", _input.image)
            await self._session.send_realtime_input(
                video=types.Blob(data=buf.tobytes(), mime_type="image/jpeg")
            )
        elif isinstance(_input, FunctionCallResultInput):
            if not _input.trigger_response:
                logger.info('[DEBUG] Function call result: %s', _input)
                if _input.call_id in self._pending_tool_calls:
                    self._requires_fresh_session = True
                    logger.info(
                        "[realtime] Tool call %s intentionally unacknowledged — "
                        "fresh Gemini session required",
                        _input.call_id,
                    )
                    turn_done: threading.Event | None = getattr(self, "_turn_done", None)
                    if turn_done is not None:
                        turn_done.set()
                    recv_queue = getattr(self, "_recv_queue", None)
                    if recv_queue is not None:
                        recv_queue.put(TurnDoneEvent())
                return

            try:
                parsed: Any = json.loads(_input.output)
            except (json.JSONDecodeError, TypeError):
                parsed = {"result": _input.output}
            if not isinstance(parsed, dict):
                parsed = {"result": parsed}
            if _input.image is not None:
                # Keep a captured frame attached to its original async look.
                # A separate realtime video frame after activityEnd may only
                # enter the next user turn, leaving this tool without an image.
                if (not self.supports_look_continuation
                        or _input.call_id not in self._pending_tool_calls
                        or getattr(self, "_pending_tool_names", {}).get(_input.call_id) != "look"
                        or _input.call_id in getattr(self, "_cancelled_look_calls", set())):
                    logger.info("[realtime] Ignoring stale or unsupported look image result")
                    return
                ok, encoded = cv2.imencode(".jpg", _input.image)
                if not ok:
                    raise ValueError("Could not encode look image")
                # google-genai 2.12.1 send_tool_response leaves bytes in parts
                # unencoded and raises TypeError. Use the documented wire shape
                # only for this multimodal result; other ACKs retain the SDK.
                response = {
                    "id": _input.call_id, "name": "look", "response": parsed,
                    "parts": [{"inlineData": {
                        "mimeType": "image/jpeg",
                        "data": base64.b64encode(encoded.tobytes()).decode("ascii"),
                    }}],
                }
                target_session = self._session
                await target_session._ws.send(json.dumps({
                    "toolResponse": {"functionResponses": [response]},
                }))
                logger.info("[realtime] Sent look image in tool response (call_id=%s bytes=%d)",
                            _input.call_id, len(encoded))
                if self._session is target_session:
                    await self._finish_tool_ack(_input.call_id)
                return
            # NON_BLOCKING declarations do not imply scheduling support. The
            # deployed 3.8 extended-thinking backend closes with 1007 when a
            # FunctionResponse includes scheduling (device-observed 2026-09-21).
            await self._session.send_tool_response(
                function_responses=[types.FunctionResponse(
                    id=_input.call_id,
                    name=getattr(self, "_pending_tool_names", {}).get(_input.call_id),
                    response=parsed,
                )]
            )
            # Gemini accepted the response, so this call is resolved. Keeping the
            # gate until after the await avoids racing subsequent client input
            # against the tool response on the wire.
            await self._finish_tool_ack(_input.call_id)

    async def _finish_tool_ack(self, call_id: str) -> None:
        self._pending_tool_calls.discard(call_id)
        getattr(self, "_pending_tool_names", {}).pop(call_id, None)
        getattr(self, "_cancelled_look_calls", set()).discard(call_id)
        if not self._pending_tool_calls:
            pending_image = getattr(self, "_pending_image", None)
            self._clear_pending_tool_calls()
            if pending_image is not None:
                await self._async_send_input(pending_image)

    def end_audio_stream(self, *, session: object) -> bool:
        """Preserve audio/EOS ordering without ending a manual-VAD activity."""
        if (not self.available or self.audio_session is not session
                or self._vad_disabled or self.requires_fresh_session):
            return False
        self._send_queue.put(AudioStreamEndEvent(session=session))
        return True

    async def _async_end_audio_stream(self, session: object) -> None:
        # Recheck after queueing: a tool or transport swap can occur while the
        # sender drains earlier audio. Never replay EOS onto a replacement.
        self.validate_audio_session(session)
        if self._vad_disabled or self.requires_fresh_session:
            return
        await session.send_realtime_input(audio_stream_end=True)

    async def _async_commit(
        self, turn_end_queued_at: float | None = None, *, session: object | None = None,
    ) -> None:
        if session is not None:
            self.validate_audio_session(session)
        if self._session is None:
            return
        audio_session = self._session if session is None else session
        if self._vad_disabled and self._activity_started:
            if self._tool_call_pending():
                logger.debug(
                    "[realtime] Suppressed activityEnd while tool call(s) %s are pending",
                    sorted(self._pending_tool_calls),
                )
                self._activity_started = False
                return
            await audio_session.send_realtime_input(activity_end=types.ActivityEnd())
            if session is not None:
                self.validate_audio_session(session)
            self._activity_end_sent_at = time.monotonic()
            self._activity_started = False
            self._turn_done.clear()
            if not app_config.LIVE_MODE and getattr(self, "_skip_stale_turn_done", False):
                replay_signal = getattr(self, "_replay_commit_signal", None)
                if replay_signal is not None:
                    replay_signal.set()
            if turn_end_queued_at is not None:
                logger.info(
                    "[realtime] Turn timing: local_end->activityEnd_sent=%.0fms",
                    (self._activity_end_sent_at - turn_end_queued_at) * 1000,
                )
            else:
                logger.debug("[realtime] Sent activityEnd (manual VAD)")

    def _observe_user_speech(self, *, endpoint_at: float | None = None, transcript: str = "",
                             transcript_finished: bool = False) -> None:
        """Publish one input key, enriching it only with an actual VAD endpoint."""
        if not app_config.LIVE_MODE:
            return
        if transcript_finished and not transcript and endpoint_at is None:
            # Completion-only metadata cannot invent an input turn.
            if not getattr(self, "_live_user_turn_id", ""):
                return
        if not getattr(self, "_live_user_turn_id", ""):
            self._live_user_turn_id = "gemini-" + uuid4().hex
            self._live_speech_emitted = False
        if (getattr(self, "_live_speech_emitted", False) and endpoint_at is None
                and not transcript and not transcript_finished):
            return
        self._live_speech_emitted = True
        self._recv_queue.put(OutputEvent(
            gen=getattr(self, "_turn_gen", 0),
            output=UserSpeechOutput(
                turn_id=self._live_user_turn_id,
                transcript=transcript,
                transcript_finished=transcript_finished,
                user_turn_id=self._live_user_turn_id,
                endpoint_at=endpoint_at,
                # This stamp drives existing voice cues, but is only the
                # RECEIVE time, not speech end on the input audio timeline.
                method="server_vad_receive" if endpoint_at is not None else "provider_transcript",
            ),
        ))

    async def _async_receive_turn(self) -> None:
        """Read one full turn from the session, put outputs on _recv_queue."""
        if self._session is None:
            return
        self._first_audio_received = False
        # A new receive turn is a new generation. NOT sufficient on its own:
        # `interrupted` does NOT end this loop (only turn_complete and
        # generation_complete return), so a barge-in has to bump it too or the
        # audio before and after the interruption would share a generation.
        #
        # getattr: tests drive this loop on agents built without __init__, the
        # same way _idle_parked / _requires_fresh_session are read elsewhere.
        self._turn_gen = getattr(self, "_turn_gen", 0) + 1
        logger.info("[realtime] Gemini turn %d receive loop start", self._turn_gen)
        valid_transcription_chunk_cnt = 0

        execution_interrupted = False
        replay_response = False
        replay_boundary_pending = False
        replay_signal = asyncio.Event()
        self._replay_commit_signal = replay_signal
        pending_message: asyncio.Task | None = None
        response_user_turn_id: str | None = None
        # NON_BLOCKING tools (extended-thinking) let the model speak a filler,
        # send turn_complete, THEN emit the tool call. Ending the turn at
        # turn_complete drops that late delegate/reject (#453). Hold the loop a
        # short grace after the terminal to catch a trailing tool call, cut short
        # when a routing call arrives. Filler output is still queued immediately.
        _grace_s: float = app_config.REALTIME_NONBLOCKING_TOOL_GRACE_S
        # getattr: tests drive this loop on agents built without __init__ (no _config).
        _grace_model: str = getattr(getattr(self, "_config", None), "model", "") or ""
        _requires_outcome = "extended-thinking" in _grace_model
        _grace_on: bool = _grace_s > 0 and _requires_outcome
        self._progress_watchdog_enabled = _requires_outcome and not app_config.LIVE_MODE
        _outcome_received = False
        _routing_received = False
        _direct_answer_confirmed = False
        interaction_status = None
        interaction_deadline = 0.0
        _turn_transcript = ""
        _spoken_response = ""
        _initial_speech = False
        _empty_terminal_pending = False
        _initial_active_until = 0.0
        _continuation_active_until = 0.0
        _last_continuation_output_until = 0.0
        _progress_until = 0.0
        _search_context: list[str] = []
        _outcome_check: asyncio.Task[bool | None] | None = None
        _continuation_check: asyncio.Task[bool | None] | None = None
        _continuation: list[OutputEvent] = []
        _continuation_text = ""
        _continuation_bytes = 0
        _continuation_overflow = False
        _outcome_deadline = 0.0
        _grounded = False
        _grace_deadline: float = 0.0
        _deferred_finalize: Callable[[], Any] | None = None
        _receiver = self._session.receive().__aiter__()

        def _log_timing(event: str) -> None:
            now = time.monotonic()
            committed_at = getattr(self, "_committed_at", 0.0)
            logger.info(
                "[realtime][timing] %s gen=%s since_latest_commit_s=%.3f "
                "progress_remaining_s=%.3f output_remaining_s=%.3f",
                event, self._turn_gen,
                now - committed_at if committed_at else -1.0,
                max(0.0, _progress_until - now),
                max(0.0, max(_initial_active_until, _continuation_active_until) - now),
            )

        def _note_progress() -> None:
            nonlocal _progress_until
            committed_at = getattr(self, "_committed_at", 0.0)
            if (committed_at and _requires_outcome and not app_config.LIVE_MODE
                    and app_config.REALTIME_PROGRESS_TIMEOUT_S > 0):
                first_progress = not _progress_until
                _progress_until = committed_at + app_config.REALTIME_PROGRESS_TIMEOUT_S
                self.allow_progress_until(_progress_until)
                if first_progress:
                    _log_timing("first_verified_progress")

        def _handoff_context() -> str:
            parts = list(_search_context)
            if _spoken_response.strip():
                parts.append("Already spoken to user: " + _spoken_response[:2000])
            return "\n".join(parts)[:6000]

        def _start_outcome_check() -> None:
            nonlocal _outcome_check, _outcome_deadline
            if (_outcome_check is not None or _routing_received or execution_interrupted
                    or not _spoken_response.strip()):
                return
            from hal.realtime.response_outcome import spoken_response_complete
            request = getattr(self, "_user_transcript", "") or _turn_transcript
            if not _outcome_deadline:
                _outcome_deadline = time.monotonic() + app_config.REALTIME_OUTCOME_TIMEOUT_S
            answer = _spoken_response
            grounded = _grounded
            async def check() -> bool | None:
                timeout = max(0.0, _outcome_deadline - time.monotonic())
                return await asyncio.wait_for(spoken_response_complete(
                    request, answer, grounded=grounded, timeout=timeout,
                ), timeout=timeout)
            _outcome_check = asyncio.create_task(check())

        async def _check_continuation() -> None:
            nonlocal _continuation_check, _outcome_deadline
            if not _continuation_text or _continuation_overflow or _routing_received:
                return
            from hal.realtime.response_outcome import spoken_response_complete
            if _continuation_check is not None:
                _continuation_check.cancel()
                await asyncio.gather(_continuation_check, return_exceptions=True)
            request = getattr(self, "_user_transcript", "") or _turn_transcript
            # A later answer needs its own bounded classification window. The
            # first terminal may have had no speech, or its check expired while
            # a real tool was working.
            _outcome_deadline = min(
                time.monotonic() + app_config.REALTIME_OUTCOME_TIMEOUT_S,
                _last_continuation_output_until,
            )
            last_chunk_at = (
                _last_continuation_output_until - app_config.REALTIME_RECV_QUEUE_TIMEOUT_S)
            if _progress_until and last_chunk_at <= _progress_until:
                _outcome_deadline = min(_outcome_deadline, _progress_until)
            self.allow_output_until(_outcome_deadline)
            answer = _spoken_response + _continuation_text
            grounded = _grounded

            async def check() -> bool | None:
                timeout = max(0.0, _outcome_deadline - time.monotonic())
                return await asyncio.wait_for(spoken_response_complete(
                    request, answer, grounded=grounded, timeout=timeout,
                ), timeout=timeout)

            _continuation_check = asyncio.create_task(check())

        async def _fallback_needed(delayed_playback_ack: bool = False) -> bool:
            if getattr(self, "_reject_followup_barrier", False):
                return False
            if interaction_status == "IDLE":
                # Extended Thinking has finished the whole interaction, not
                # merely a filler utterance. Do not let a second model's
                # availability override this provider execution boundary.
                # Empty output or unresolved local work still needs recovery.
                return (_requires_outcome and not _routing_received
                        and not execution_interrupted and not delayed_playback_ack
                        and (not _spoken_response.strip() or bool(self._pending_tool_calls)
                             or bool(_continuation) or _continuation_overflow))
            independently_complete = None
            continuation_released = False
            if _outcome_check is not None:
                if _routing_received or execution_interrupted or delayed_playback_ack:
                    _outcome_check.cancel()
                else:
                    try:
                        if _progress_until and not _outcome_check.done():
                            independently_complete = await asyncio.wait_for(
                                _outcome_check,
                                timeout=max(0.0, _progress_until - time.monotonic()),
                            )
                        else:
                            independently_complete = await _outcome_check
                    except asyncio.TimeoutError:
                        logger.warning("[realtime] Spoken outcome check timed out")
                        independently_complete = None
                logger.info("[realtime] Spoken outcome check: %s",
                            "complete" if independently_complete is True else
                            "incomplete" if independently_complete is False else "unconfirmed")
            if _continuation_check is not None:
                if (_routing_received or execution_interrupted or delayed_playback_ack
                        or (_initial_speech and independently_complete is not False)
                        or _continuation_overflow):
                    _continuation_check.cancel()
                else:
                    try:
                        continuation_complete = await _continuation_check
                    except asyncio.TimeoutError:
                        logger.warning("[realtime] Continuation outcome check timed out")
                        continuation_complete = None
                    # A completed initial answer keeps the old duplicate guard.
                    # Only an empty initial response or a confirmed incomplete
                    # reply followed by a checked, terminal-ended continuation can
                    # be released, and a trailing delegate always wins first.
                    if continuation_complete is True:
                        for event in _continuation:
                            self._recv_queue.put(event)
                        independently_complete = True
                        continuation_released = True
                        logger.info("[realtime] Releasing confirmed continuation after initial response")
                await asyncio.gather(_continuation_check, return_exceptions=True)
            if _outcome_check is not None:
                await asyncio.gather(_outcome_check, return_exceptions=True)
            if _continuation:
                _log_timing("continuation_released" if continuation_released else "continuation_discarded")
                logger.info("[realtime] Continuation decision: released=%s routing=%s audio_bytes=%d text_chars=%d",
                            continuation_released, _routing_received,
                            _continuation_bytes, len(_continuation_text))
            confirmed = independently_complete if independently_complete is not None else _outcome_received
            if (_continuation or _continuation_overflow) and independently_complete is not True:
                # A provider confirmation must not claim that withheld speech
                # reached the user when the independent check was unavailable.
                confirmed = False
            return (_requires_outcome and not _routing_received and not confirmed
                    and not execution_interrupted and not delayed_playback_ack)

        async def cancel_pending_read() -> None:
            nonlocal pending_message
            if pending_message is not None:
                pending_message.cancel()
                await asyncio.gather(pending_message, return_exceptions=True)
                pending_message = None

        async def _finalize_turn_complete(delayed_playback_ack: bool) -> None:
            await cancel_pending_read()
            transcript = getattr(self, "_user_transcript", "") or _turn_transcript
            fallback = await _fallback_needed(delayed_playback_ack)
            if fallback:
                logger.warning("[realtime] No confirmed outcome after NON_BLOCKING response — forwarding to main")
                if _initial_speech or _continuation or _progress_until:
                    self._requires_fresh_session = True
            self._awaiting_playback_turn_complete = False
            self._first_audio_received = False
            self._user_transcript = ""
            self._turn_done.set()
            self._recv_queue.put(TurnDoneEvent(
                execution_completed=(not execution_interrupted and not delayed_playback_ack and not fallback
                                     and not getattr(self, "_reject_followup_barrier", False)),
                fallback_to_main=fallback,
                user_transcript=transcript,
                handoff_context=_handoff_context() if fallback else "",
                user_turn_id=("" if delayed_playback_ack else response_user_turn_id
                              if response_user_turn_id is not None
                              else getattr(self, "_live_user_turn_id", "")),
            ))
            if not delayed_playback_ack and (response_user_turn_id is None or (
                    getattr(self, "_live_user_turn_id", "") == response_user_turn_id)):
                self._live_user_turn_id = ""
                self._live_speech_emitted = False

        async def _finalize_generation_complete() -> None:
            await cancel_pending_read()
            transcript = getattr(self, "_user_transcript", "") or _turn_transcript
            fallback = await _fallback_needed()
            if fallback:
                logger.warning("[realtime] No confirmed outcome after NON_BLOCKING response — forwarding to main")
                if _initial_speech or _continuation or _progress_until:
                    self._requires_fresh_session = True
            self._user_transcript = ""
            self._awaiting_playback_turn_complete = True
            self._first_audio_received = False
            self._turn_done.set()
            self._recv_queue.put(TurnDoneEvent(
                execution_completed=(not execution_interrupted and not fallback
                                     and not getattr(self, "_reject_followup_barrier", False)),
                fallback_to_main=fallback,
                user_transcript=transcript,
                handoff_context=_handoff_context() if fallback else "",
                user_turn_id=(response_user_turn_id if response_user_turn_id is not None
                              else getattr(self, "_live_user_turn_id", "")),
            ))
            if response_user_turn_id is None or (
                    getattr(self, "_live_user_turn_id", "") == response_user_turn_id):
                self._live_user_turn_id = ""
                self._live_speech_emitted = False

        async def read_message(timeout: float | None, check=None):
            # Keep a single socket read alive across a replay commit or an
            # outcome-check wakeup. Cancelling it would lose the SDK iterator.
            nonlocal pending_message
            if pending_message is None:
                pending_message = asyncio.create_task(_receiver.__anext__())
            replay_wait = asyncio.create_task(replay_signal.wait())
            try:
                waits = {pending_message, replay_wait}
                if check is not None and not check.done():
                    waits.add(check)
                done, _ = await asyncio.wait(
                    waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                if replay_signal.is_set():
                    return None
                if pending_message in done:
                    ready, pending_message = pending_message, None
                    return ready.result()
                if done:
                    return None  # Re-evaluate the deadline after classification.
                raise asyncio.TimeoutError
            except BaseException:
                if pending_message is not None:
                    pending_message.cancel()
                    await asyncio.gather(pending_message, return_exceptions=True)
                    pending_message = None
                raise
            finally:
                replay_wait.cancel()
                try:
                    await asyncio.gather(replay_wait, return_exceptions=True)
                except BaseException:
                    await cancel_pending_read()
                    raise

        while True:
            if replay_signal.is_set():
                replay_signal.clear()
                # A successful explicit look replay replaces the filler response
                # even when the provider does not emit interrupted. Retire its
                # classification and deadlines before accepting replay output.
                try:
                    for check in (_outcome_check, _continuation_check):
                        if check is not None:
                            check.cancel()
                            await asyncio.gather(check, return_exceptions=True)
                except BaseException:
                    await cancel_pending_read()
                    raise
                _outcome_check = _continuation_check = None
                _deferred_finalize = None
                _grace_deadline = _outcome_deadline = 0.0
                _initial_active_until = _continuation_active_until = 0.0
                _last_continuation_output_until = _progress_until = 0.0
                _outcome_received = _routing_received = False
                _direct_answer_confirmed = _initial_speech = False
                _empty_terminal_pending = False
                _spoken_response = _continuation_text = ""
                _continuation.clear()
                _continuation_bytes = 0
                _continuation_overflow = _grounded = False
                _search_context.clear()
                execution_interrupted = False
                interaction_status = None
                interaction_deadline = 0.0
                replay_response = replay_boundary_pending = True
                self._skip_stale_turn_done = False
                self._turn_gen += 1
                valid_transcription_chunk_cnt = 0
                self._first_audio_received = False
                self._awaiting_playback_turn_complete = False
                _note_progress()
                _log_timing("look_replay_response_started")
            try:
                if interaction_status == "IN_PROGRESS":
                    remaining = interaction_deadline - time.monotonic()
                    if remaining <= 0:
                        logger.warning("[realtime] Interaction stalled while IN_PROGRESS")
                        self._requires_fresh_session = True
                        await _finalize_generation_complete()
                        return
                    message = await read_message(remaining)
                elif _deferred_finalize is not None:
                    # Progress buys time only for an unfinished outcome. A real
                    # completed answer still observes the ordinary routing grace.
                    progress_deadline = _progress_until
                    routing_deadline = (
                        min(_grace_deadline, _progress_until)
                        if _progress_until else _grace_deadline
                    )
                    check = _continuation_check or _outcome_check
                    if check is not None and check.done() and not check.cancelled():
                        try:
                            if check.result() is True:
                                progress_deadline = 0.0
                        except Exception:
                            pass
                    deadline = max(routing_deadline, progress_deadline,
                                   _continuation_active_until)
                    _remaining = deadline - time.monotonic()
                    if _remaining <= 0:
                        logger.info("[realtime] NON_BLOCKING grace expired (%.2fs, pending_tools=%d)",
                                    _grace_s, len(self._pending_tool_calls))
                        _log_timing("deferred_wait_expired")
                        await _deferred_finalize()
                        return
                    message = await read_message(_remaining, check)
                elif _progress_until:
                    remaining = max(_progress_until, _initial_active_until) - time.monotonic()
                    if remaining <= 0:
                        await _finalize_generation_complete()
                        return
                    message = await read_message(remaining)
                else:
                    message = await read_message(None)
                if message is None:
                    continue
            except StopAsyncIteration:
                if _deferred_finalize is not None or replay_response or interaction_status == "IN_PROGRESS":
                    # The SDK receive() iterator ends at turn_complete, not at
                    # socket close. Read the next iterator on the SAME session
                    # while preserving this logical turn and its deadline.
                    _receiver = self._session.receive().__aiter__()
                    await asyncio.sleep(0)
                    continue
                return
            except (asyncio.TimeoutError, TimeoutError):
                if interaction_status == "IN_PROGRESS":
                    logger.warning("[realtime] Interaction stalled while IN_PROGRESS")
                    self._requires_fresh_session = True
                    await _finalize_generation_complete()
                    return
                if _deferred_finalize is not None:
                    logger.info("[realtime] NON_BLOCKING grace expired (%.2fs, pending_tools=%d)",
                                _grace_s, len(self._pending_tool_calls))
                    _log_timing("deferred_wait_expired")
                    await _deferred_finalize()
                    return
                if _progress_until:
                    await _finalize_generation_complete()
                    return
                raise
            # Liveness for the silent-turn watchdog: most of what arrives here
            # never reaches _recv_queue (thought parts, grounding metadata,
            # usage-only frames), and without this a turn that is busy grounding
            # is indistinguishable from one the model abandoned. See
            # RealtimeVoiceAgent.note_server_activity.
            self.note_server_activity()
            if app_config.LIVE_MODE and getattr(self, "_reject_followup_barrier", False):
                content = message.server_content
                incoming = getattr(content, "input_transcription", None)
                activity = getattr(message, "voice_activity", None)
                fresh_input = (
                    getattr(activity, "voice_activity_type", None) == "ACTIVITY_START"
                    or bool((getattr(incoming, "text", None) or "").strip())
                )
                if fresh_input:
                    self._reject_followup_barrier = False
                    self._live_user_turn_id = ""
                    self._live_speech_emitted = False
                    self._user_transcript = ""
                    response_user_turn_id = None
                    _routing_received = _outcome_received = False
                    _empty_terminal_pending = False
                    _continuation.clear()
                    _continuation_text = ""
                    _continuation_bytes = 0
                    _continuation_overflow = False
                    self._awaiting_playback_turn_complete = False
                else:
                    # ACK-triggered speech has no new user owner. Keep the
                    # barrier across receive iterators and protocol terminals;
                    # do not feed it to continuation/outcome/fallback machinery.
                    cancellation = getattr(message, "tool_call_cancellation", None)
                    if cancellation is not None:
                        self._invalidate_look_images(getattr(cancellation, "ids", ()) or ())
                    update = getattr(message, "session_resumption_update", None)
                    if update is not None and update.new_handle:
                        self._resumption_handle = update.new_handle
                    if getattr(message, "go_away", None):
                        raise ConnectionClosed(None, None)
                    tool_call = getattr(message, "tool_call", None)
                    for call in (getattr(tool_call, "function_calls", None) or ()):
                        await self._session.send_tool_response(function_responses=[
                            types.FunctionResponse(id=call.id, name=call.name,
                                                   response={"result": "ignored: rejected turn"})
                        ])
                    if content is not None and (
                        getattr(content, "turn_complete", False)
                        or getattr(content, "generation_complete", False)
                        or getattr(content, "interaction_status", None) == "IDLE"
                    ):
                        await _finalize_turn_complete(False)
                        return
                    continue
            cancellation = getattr(message, "tool_call_cancellation", None)
            if cancellation is not None:
                self._invalidate_look_images(getattr(cancellation, "ids", ()) or ())
            activity = getattr(message, "voice_activity", None)
            activity_type = getattr(activity, "voice_activity_type", None)
            if activity_type == "ACTIVITY_START":
                self._live_user_turn_id = ""
                self._live_speech_emitted = False
            elif activity_type == "ACTIVITY_END":
                self._observe_user_speech(endpoint_at=time.monotonic())
            if message.usage_metadata:
                # Per-turn token bill. prompt_token_count is the input CONTEXT
                # billed this turn — it grows as a long-lived session accumulates
                # history and should drop sharply right after an idle session
                # recycle (see orchestrator._mark_turn_start). Grep
                # "[realtime] Gemini usage" to confirm the reset is cutting cost.
                # Checked FIRST: Gemini ships usage_metadata on the SAME message as
                # turn_complete, and the server_content branch returns on
                # turn_complete — so a check placed after it never runs.
                um = message.usage_metadata
                # Per-model rate table (see _GEMINI_RATES above). Resolved by model
                # name so 2.5 native-audio (cheaper text) and 3.1 Live are billed at
                # their own rates; unknown models fall back to a cost ceiling.
                rates = _gemini_rates_for(self._config.model)
                parts, cost, attributed = [], 0.0, {"in": 0, "out": 0}
                for direction, details in (
                    ("in", um.prompt_tokens_details),
                    ("out", um.response_tokens_details),
                ):
                    for d in details or []:
                        mod = getattr(d.modality, "name", str(d.modality))
                        tok = d.token_count or 0
                        attributed[direction] += tok
                        c = tok * rates.get((direction, mod), 0.0) / 1_000_000
                        cost += c
                        parts.append("%s_%s=%d($%.5f)" % (direction, mod.lower(), tok, c))
                # Tokens Google counted but didn't tag with a modality (system /
                # thinking) — unpriced here, so est is a floor.
                unattr_in = (um.prompt_token_count or 0) - attributed["in"]
                unattr_out = (um.response_token_count or 0) - attributed["out"]
                # Implicit context caching (on by default for Gemini 2.5+/3.1) re-bills
                # the cached prefix — our ~8k-token system-instruction floor — at a 90%
                # discount. `cost` above charges every prompt token at full rate, so
                # subtract the saving on cached tokens to get the REAL estimate. cached
                # tokens are text-in (the floor is text). cached=0 every turn means the
                # cache is not hitting (e.g. session churn) — that's the cost red flag.
                cached = getattr(um, "cached_content_token_count", 0) or 0
                cost_cached = max(0.0, cost - cached * rates[("in", "TEXT")] * 0.90 / 1_000_000)
                usage_logger.info(
                    "[realtime] Gemini usage: model=%s %s +unattr(%din/%dout) | "
                    "cached=%dtok total=%dtok est_full>=$%.5f est_cached>=$%.5f",
                    self._config.model,
                    " ".join(parts) or "-", unattr_in, unattr_out, cached,
                    um.total_token_count or 0, cost, cost_cached,
                )

            reject_in_message = app_config.LIVE_MODE and any(
                call.name == "reject_turn"
                for call in (message.tool_call.function_calls if message.tool_call else [])
            )
            if message.server_content:
                content = message.server_content

                # Diagnostic: did Google Search grounding fire this turn? Grounding
                # chunks (web content) get injected into the session context and
                # re-billed as input every later turn — the suspected cause of a
                # sudden persistent in_text jump. Logs the queries + chunk count so
                # we can correlate a search with the cost bump.
                gm = getattr(content, "grounding_metadata", None)
                if gm is not None:
                    queries = list(getattr(gm, "web_search_queries", None) or [])
                    chunks = getattr(gm, "grounding_chunks", None) or []
                    _grounded = _grounded or bool(chunks)
                    if queries or chunks:
                        _note_progress()
                    for query in queries[:3]:
                        entry = "Search query: " + str(query)[:300]
                        if entry not in _search_context and len(_search_context) < 12:
                            _search_context.append(entry)
                    for chunk in chunks[:5]:
                        web = getattr(chunk, "web", None)
                        if web is not None:
                            entry = "Search source: " + " | ".join(
                                str(getattr(web, key, "") or "")[:800]
                                for key in ("title", "uri", "snippet"))
                            if entry not in _search_context and len(_search_context) < 12:
                                _search_context.append(entry)
                    logger.info(
                        "[realtime][grounding] Google Search metadata received (search start unknown): queries=%s chunks=%d",
                        queries[:3], len(chunks),
                    )
                    _log_timing("grounding_received")
                    # chunks=0 on turns that clearly ran a search (measured on
                    # lamp-0c89 04/09/2026: 2 of 3 grounded turns) has two very
                    # different causes and the count alone cannot separate them:
                    # the search really returned nothing, or the metadata reached
                    # us stripped (the proxy forwards web_search_queries, so the
                    # object itself survives — the question is what else does).
                    # This dumps every field of the metadata verbatim, once per
                    # turn, so the next grounded turn answers it from evidence.
                    # Off by default: it is verbose and only useful while
                    # investigating. HAL_REALTIME_GROUNDING_DEBUG=true to arm.
                    if app_config.REALTIME_GROUNDING_DEBUG:
                        try:
                            fields = {
                                name: getattr(gm, name, None)
                                for name in (
                                    "web_search_queries",
                                    "grounding_chunks",
                                    "grounding_supports",
                                    "search_entry_point",
                                    "retrieval_metadata",
                                    "retrieval_queries",
                                    "google_maps_widget_context_token",
                                )
                            }
                            logger.info(
                                "[realtime][grounding][debug] set=%s | %s",
                                sorted(k for k, v in fields.items() if v),
                                {k: repr(v)[:300] for k, v in fields.items()},
                            )
                        except Exception as e:
                            logger.warning(
                                "[realtime][grounding][debug] dump failed: %s", e
                            )

                if replay_boundary_pending:
                    has_reply = bool(content.output_transcription and content.output_transcription.text) or any(
                        not getattr(part, "thought", False) and part.inline_data and part.inline_data.data
                        for part in (content.model_turn.parts if content.model_turn else []))
                    if has_reply:
                        replay_boundary_pending = False
                    elif (content.interrupted or content.turn_complete
                          or getattr(content, "generation_complete", False)
                          or getattr(content, "interaction_status", None) == "IDLE"):
                        # The cancelled filler may end with interrupted, a late
                        # terminal, both, or neither. Retire that boundary here;
                        # never discard the replay's own TurnDoneEvent.
                        if content.turn_complete:
                            replay_boundary_pending = False
                        continue

                status = getattr(content, "interaction_status", None)
                if _requires_outcome and status in {"IN_PROGRESS", "IDLE"}:
                    self.supports_look_continuation = True
                    first_status = interaction_status is None
                    interaction_status = status
                    logger.info("[realtime] interaction_status=%s gen=%s", status, self._turn_gen)
                    if first_status:
                        # The SDK terminal may precede this status. Retire its
                        # provisional filler classification, not the user's turn.
                        for check in (_outcome_check, _continuation_check):
                            if check is not None:
                                check.cancel()
                                await asyncio.gather(check, return_exceptions=True)
                        _outcome_check = _continuation_check = None
                        _outcome_deadline = 0.0
                        _deferred_finalize = None
                        _direct_answer_confirmed = False
                        _outcome_received = _routing_received
                        routing_in_message = any(
                            fc.name in {"delegate_to_main", "reject_turn", "end_conversation"}
                            for fc in (message.tool_call.function_calls if message.tool_call else []))
                        if (_continuation and not _continuation_overflow
                                and not _empty_terminal_pending
                                and not _routing_received and not routing_in_message
                                and not execution_interrupted and not content.interrupted):
                            for event in _continuation:
                                self._recv_queue.put(event)
                            _spoken_response += _continuation_text
                            _initial_speech = bool(_spoken_response.strip())
                            _continuation.clear()
                            _continuation_text = ""
                            _continuation_bytes = 0
                    if first_status and status == "IN_PROGRESS":
                        interaction_deadline = time.monotonic() + max(
                            app_config.REALTIME_TURN_MAX_SILENCE_S,
                            app_config.REALTIME_RECV_QUEUE_TIMEOUT_S)
                        self.allow_progress_until(interaction_deadline)
                if interaction_status == "IN_PROGRESS" and (
                        content.model_turn or content.output_transcription):
                    interaction_deadline = time.monotonic() + max(
                        app_config.REALTIME_TURN_MAX_SILENCE_S,
                        app_config.REALTIME_RECV_QUEUE_TIMEOUT_S)
                    self.allow_progress_until(interaction_deadline)

                interrupted_user_turn_id = ""
                if content.interrupted and app_config.LIVE_MODE:
                    interrupted_user_turn_id = (
                        response_user_turn_id if response_user_turn_id is not None
                        else getattr(self, "_live_user_turn_id", "")
                    )
                    response_user_turn_id = interrupted_user_turn_id
                    self._live_user_turn_id = ""
                    self._live_speech_emitted = False

                _in_tx = getattr(content, "input_transcription", None)
                if _in_tx is not None and (_in_tx.text or getattr(_in_tx, "finished", False) is True):
                    self._observe_user_speech(
                        transcript=_in_tx.text or "",
                        transcript_finished=getattr(_in_tx, "finished", False) is True,
                    )

                # The initial response streams immediately. Post-terminal output
                # stays off-speaker until an incomplete initial reply and a valid
                # continuation are confirmed without a trailing routing tool.
                accept_speech = (not execution_interrupted and not _routing_received
                                 and not _empty_terminal_pending and not reject_in_message
                                 and (interaction_status is not None or (
                                     not _direct_answer_confirmed and not (
                                         _requires_outcome and _deferred_finalize is not None))))
                if (not accept_speech and not _direct_answer_confirmed
                        and not _routing_received and not execution_interrupted
                        and not content.interrupted and not _continuation_overflow
                        and not reject_in_message):
                    # Hold post-terminal speech until the routing grace ends.
                    # It can be the real answer after a filler, or unwanted ACK
                    # chatter. Never let it reach the speaker before deciding.
                    if (_empty_terminal_pending and response_user_turn_id is None
                            and (content.model_turn or content.output_transcription)):
                        response_user_turn_id = getattr(self, "_live_user_turn_id", "")
                    held_outputs = []
                    for part in (content.model_turn.parts if content.model_turn else []):
                        if not getattr(part, "thought", False) and part.inline_data and part.inline_data.data:
                            _continuation_bytes += len(part.inline_data.data)
                            held_outputs.append(AudioOutput(
                                user_turn_id=response_user_turn_id or "",
                                audio=pcm16_bytes_to_float32(part.inline_data.data),
                            ))
                    if content.output_transcription and content.output_transcription.text:
                        text = content.output_transcription.text
                        _continuation_text += text
                        held_outputs.append(TextOutput(text=text, user_turn_id=response_user_turn_id or ""))
                    if _continuation_bytes > 2_000_000 or len(_continuation_text) > 16_000:
                        _continuation_overflow = True
                        _continuation.clear()
                        logger.warning("[realtime] Continuation buffer limit reached; preserving fallback")
                    elif held_outputs:
                        # Audio/text chunks, unlike metadata heartbeats, prove
                        # that a continuation is still being generated.
                        _continuation_active_until = (
                            time.monotonic() + app_config.REALTIME_RECV_QUEUE_TIMEOUT_S)
                        _last_continuation_output_until = _continuation_active_until
                        self.allow_output_until(_continuation_active_until)
                        if not _continuation:
                            _log_timing("first_continuation_held")
                        _continuation.extend(OutputEvent(gen=self._turn_gen, output=o) for o in held_outputs)
                        # New chunks invalidate a check of an earlier terminal.
                        if _continuation_check is not None:
                            _continuation_check.cancel()
                            await asyncio.gather(_continuation_check, return_exceptions=True)
                            _continuation_check = None
                        logger.debug("[realtime] Holding continuation: audio_bytes=%d text_chars=%d",
                                     _continuation_bytes, len(_continuation_text))
                has_model_output = accept_speech and bool(
                    content.model_turn or content.output_transcription
                )
                if has_model_output:
                    self._awaiting_playback_turn_complete = False
                if response_user_turn_id is None and has_model_output:
                    # Transcription can arrive after model output. Freeze unknown
                    # ownership instead of attributing that response to a later input.
                    response_user_turn_id = getattr(self, "_live_user_turn_id", "")

                if content.model_turn and content.model_turn.parts and accept_speech:
                    for part in content.model_turn.parts:
                        # Skip reasoning parts: Gemini flags thought parts with
                        # part.thought=True. Emitting them would speak/show the
                        # model's internal reasoning. Belt-and-suspenders with
                        # include_thoughts=False in the live config.
                        if getattr(part, "thought", False):
                            continue
                        if part.inline_data and part.inline_data.data:
                            _initial_speech = True
                            _initial_active_until = (
                                time.monotonic() + app_config.REALTIME_RECV_QUEUE_TIMEOUT_S)
                            self.allow_output_until(_initial_active_until)
                            if not self._first_audio_received:
                                self._first_audio_received = True
                                now = time.monotonic()
                                last_audio_sent_at = self._last_audio_sent_at
                                activity_end_sent_at = self._activity_end_sent_at
                                if last_audio_sent_at is not None:
                                    latency_ms: float = (now - last_audio_sent_at) * 1000
                                    if activity_end_sent_at is not None:
                                        logger.info(
                                            "[realtime] Response latency: %.0fms "
                                            "(last_audio_sent->first_audio; "
                                            "activityEnd_sent->first_audio=%.0fms)",
                                            latency_ms,
                                            (now - activity_end_sent_at) * 1000,
                                        )
                                    else:
                                        logger.info(
                                            "[realtime] Response latency: %.0fms "
                                            "(last_audio_sent->first_audio; server VAD)",
                                            latency_ms,
                                        )
                                self._last_audio_sent_at = None
                                self._activity_end_sent_at = None
                            self._recv_queue.put(
                                OutputEvent(
                                    gen=getattr(self, "_turn_gen", 0),
                                    output=AudioOutput(
                                        user_turn_id=response_user_turn_id or "",
                                        audio=pcm16_bytes_to_float32(
                                            part.inline_data.data
                                        )
                                    ),
                                )
                            )
                        elif part.text:
                            # In AUDIO modality with output_audio_transcription
                            # enabled (always, see _build_config), the spoken reply
                            # arrives via output_transcription below. Gemini also
                            # occasionally puts the SAME reply into a model_turn text
                            # part (same quirk as thought parts leaking, filtered
                            # above) — emitting it here too double-speaks the whole
                            # turn (verified on-device 2026-06-29: text part arrived
                            # BEFORE first audio, then output_transcription repeated
                            # it). Skip it; output_transcription is the source of
                            # truth for the spoken text + [HANDLED] transcript.
                            continue

                if _in_tx is not None and _in_tx.text:
                    logger.info("[realtime] <<< user said: %r", _in_tx.text)
                    self._user_transcript = getattr(self, "_user_transcript", "") + _in_tx.text

                if (content.output_transcription and content.output_transcription.text
                        and accept_speech):
                    _initial_speech = True
                    _initial_active_until = (
                        time.monotonic() + app_config.REALTIME_RECV_QUEUE_TIMEOUT_S)
                    self.allow_output_until(_initial_active_until)
                    if not valid_transcription_chunk_cnt:
                        self._recv_queue.put(OutputEvent(
                            gen=getattr(self, "_turn_gen", 0),
                            output=InterruptedOutput(
                                reason="output_reset",
                                user_turn_id=response_user_turn_id or "",
                            ),
                        ))

                    _spoken_response += content.output_transcription.text
                    valid_transcription_chunk_cnt += 1

                    self._recv_queue.put(
                        OutputEvent(
                            gen=getattr(self, "_turn_gen", 0),
                            output=TextOutput(
                                text=content.output_transcription.text,
                                user_turn_id=response_user_turn_id or "",
                            ),
                        )
                    )

                if content.interrupted:
                    self._invalidate_look_images(self._pending_tool_calls)
                    # A late sibling ACK must not flush the old frame into the
                    # new user's interaction. Tool ids still need normal ACKs.
                    self._pending_image = None
                    execution_interrupted = True
                    logger.info(content)
                    try:
                        _mt = content.model_turn
                        _parts = list(_mt.parts) if (_mt and _mt.parts) else []
                        logger.info(
                            "[realtime][interrupt] model_turn_parts=%d text=%r audio_bytes=%d "
                            "output_transcription=%r turn_complete=%s generation_complete=%s "
                            "usage=%s",
                            len(_parts),
                            "".join((p.text or "") for p in _parts)[:200],
                            sum(
                                len(p.inline_data.data)
                                for p in _parts
                                if p.inline_data and p.inline_data.data
                            ),
                            (
                                content.output_transcription.text
                                if content.output_transcription
                                else None
                            ),
                            content.turn_complete,
                            getattr(content, "generation_complete", None),
                            bool(message.usage_metadata),
                        )
                    except Exception as _e:
                        logger.info("[realtime][interrupt] dump failed: %s", _e)

                    dropped = self._recv_queue.qsize()
                    metadata = []

                    while not self._recv_queue.empty():
                        try:
                            queued = self._recv_queue.get_nowait()
                            if isinstance(queued, OutputEvent) and (
                                isinstance(queued.output, (UserSpeechOutput, ExecutionOutput))
                                or (isinstance(queued.output, InterruptedOutput)
                                    and queued.output.reason == "server_interrupt")
                            ):
                                metadata.append(queued)
                            elif app_config.LIVE_MODE and isinstance(queued, TurnDoneEvent):
                                # Preserve metric evidence, never replay a control
                                # terminal that the original interruption path dropped.
                                metadata.append(OutputEvent(
                                    gen=self._turn_gen,
                                    output=ExecutionOutput(
                                        user_turn_id=queued.user_turn_id,
                                        execution_completed=queued.execution_completed,
                                    ),
                                ))
                        except queue.Empty:
                            break

                    for queued in metadata:
                        self._recv_queue.put(queued)
                    if app_config.LIVE_MODE:
                        self._recv_queue.put(OutputEvent(
                            gen=self._turn_gen,
                            output=InterruptedOutput(
                                reason="server_interrupt", at=time.monotonic(),
                                user_turn_id=interrupted_user_turn_id,
                            ),
                        ))
                    logger.info(
                        "[realtime] Response interrupted — dropped %d queued output(s)",
                        dropped,
                    )
                    self._first_audio_received = False
                    self._turn_done.set()

                if content.interrupted and _empty_terminal_pending:
                    _empty_terminal_pending = False
                    _continuation.clear()
                    _continuation_text = ""
                    _continuation_bytes = 0

                # A silent terminal is not an accepted answer. Extended Thinking
                # may report IN_PROGRESS next, then speech followed by rejection.
                # Keep only this post-empty-terminal path off-speaker until IDLE.
                if (app_config.LIVE_MODE and _requires_outcome
                        and interaction_status is None and not _initial_speech
                        and not content.interrupted and (content.turn_complete
                            or getattr(content, "generation_complete", False))):
                    _empty_terminal_pending = True

                if interaction_status is not None and content.interrupted:
                    await _finalize_turn_complete(False)
                    return

                if interaction_status is None and content.turn_complete and not reject_in_message:
                    _continuation_active_until = 0.0
                    delayed_playback_ack = app_config.LIVE_MODE and getattr(
                        self, "_awaiting_playback_turn_complete", False
                    )
                    logger.debug("[realtime] Turn complete")
                    # Hold for a trailing NON_BLOCKING tool call (#453) before
                    # ending the turn; _finalize_turn_complete emits TurnDoneEvent.
                    if _grace_on and not delayed_playback_ack:
                        if _deferred_finalize is None:
                            _grace_deadline = time.monotonic() + _grace_s
                            logger.info("[realtime] NON_BLOCKING grace started at turn_complete (%.2fs)", _grace_s)
                        _deferred_finalize = (
                            lambda dpa=delayed_playback_ack: _finalize_turn_complete(dpa)
                        )
                        _start_outcome_check()
                        await _check_continuation()
                        continue
                    await _finalize_turn_complete(delayed_playback_ack)
                    return

                if (interaction_status is None and not reject_in_message
                        and getattr(content, "generation_complete", False)):
                    _continuation_active_until = 0.0
                    # Gemini Live can defer turn_complete until its assumed
                    # real-time audio playback ends. HAL plays the received
                    # response itself, so waiting for that acknowledgement
                    # makes an already-spoken turn block until the consumer's
                    # silent-output watchdog expires. BLOCKING models can finish
                    # now; NON_BLOCKING models still need the tool grace below.
                    logger.debug("[realtime] Generation complete")
                    # Audio generation can finish before a NON_BLOCKING tool
                    # arrives. Neither terminal may bypass or extend the grace.
                    if _grace_on:
                        if _deferred_finalize is None:
                            _grace_deadline = time.monotonic() + _grace_s
                            logger.info("[realtime] NON_BLOCKING grace started at generation_complete (%.2fs)", _grace_s)
                            _deferred_finalize = _finalize_generation_complete
                        _start_outcome_check()
                        await _check_continuation()
                        continue
                    await _finalize_generation_complete()
                    return

            if message.tool_call and message.tool_call.function_calls:
                if any(fc.name not in {"complete_response", "delegate_to_main",
                                       "reject_turn", "end_conversation", "express_emotion"}
                       for fc in message.tool_call.function_calls):
                    _note_progress()
                # A tool call this turn — even one arriving after turn_complete on
                # a NON_BLOCKING model (the #453 case the grace window catches).
                # Close the audio gate before the events are queued: the consumer
                # runs on another thread and can dispatch a result immediately, so
                # registering the call_ids afterwards could race a clear and leave
                # a phantom entry pending until the deadline.
                self._pending_tool_calls.update(
                    fc.id or "" for fc in message.tool_call.function_calls
                )
                # FunctionResponse requires the original name as well as the id.
                # Keep this provider metadata here rather than making each tool
                # handler reconstruct it (missing names produce model error replies).
                if not hasattr(self, "_pending_tool_names"):
                    self._pending_tool_names = {}
                self._pending_tool_names.update(
                    (fc.id or "", fc.name) for fc in message.tool_call.function_calls
                )
                if response_user_turn_id is None:
                    response_user_turn_id = getattr(self, "_live_user_turn_id", "")
                user_transcript = getattr(self, "_user_transcript", "").strip() or _turn_transcript
                if user_transcript:
                    _turn_transcript = user_transcript
                self._user_transcript = ""
                for fc in message.tool_call.function_calls:
                    logger.info("[realtime] Function call: %s (call_id=%s, after_terminal=%s)",
                                fc.name, fc.id, _deferred_finalize is not None)
                    if _requires_outcome and fc.name == "complete_response":
                        _continuation_active_until = 0.0
                        await _check_continuation()
                        # An explicit direct-answer outcome, never an inferred
                        # success from audio or a terminal alone. Ack normally;
                        # this backend does not support scheduling=SILENT.
                        await self._session.send_tool_response(function_responses=[
                            types.FunctionResponse(id=fc.id, name=fc.name,
                                                   response={"result": "recorded"})
                        ])
                        await self._finish_tool_ack(fc.id or "")
                        if interaction_status is not None:
                            logger.info("[realtime] complete_response is advisory; awaiting server IDLE")
                            continue
                        if not (_spoken_response.strip() or _continuation_text.strip()):
                            # NON_BLOCKING tools can arrive before their answer.
                            # A receipt without speech must not mute the answer
                            # still in flight or count as completed execution.
                            logger.info(
                                "[realtime] Early complete_response acknowledged — "
                                "awaiting answer text before confirming completion"
                            )
                            continue
                        _outcome_received = True
                        _direct_answer_confirmed = True
                        # The plain ACK can prompt another spoken answer. Once
                        # confirmed, retain routing events but suppress that
                        # post-confirmation speech until this turn finalizes.
                        # Keep the grace open: a delegate can follow this call in
                        # another frame (device-observed 2026-09-21, +39ms).
                        continue
                    if app_config.LIVE_MODE and fc.name == "reject_turn":
                        # Publish before the consumer can ACK. A plain ACK can
                        # cause provider speech in a later receive iteration.
                        self._reject_followup_barrier = True
                    if fc.name in {"delegate_to_main", "reject_turn", "end_conversation"}:
                        _outcome_received = True
                        _routing_received = True
                    if (fc.name == "delegate_to_main"
                            and isinstance(fc.args, dict)
                            and isinstance(fc.args.get("message"), str)
                            and fc.args["message"].strip()):
                        # Publish quarantine BEFORE the tool: its consumer may
                        # release the next capture immediately. Even a silent
                        # handoff can produce late spoken output after its ACK;
                        # it must not become the next capture's response.
                        self._requires_fresh_session = True
                    self._recv_queue.put(
                        OutputEvent(
                            gen=getattr(self, "_turn_gen", 0),
                            output=FunctionCallOutput(
                                user_turn_id=response_user_turn_id or "",
                                name=fc.name or "",
                                arguments=json.dumps(fc.args) if fc.args else "{}",
                                call_id=fc.id or "",
                                user_transcript=user_transcript,
                                handoff_context=_handoff_context() if fc.name == "delegate_to_main" else "",
                            ),
                        )
                    )
                if (reject_in_message and message.server_content is not None
                        and (message.server_content.turn_complete
                             or getattr(message.server_content, "generation_complete", False))):
                    await _finalize_turn_complete(False)
                    return
                if interaction_status is not None and _routing_received:
                    await _finalize_turn_complete(False)
                    return
                if interaction_status == "IN_PROGRESS":
                    interaction_deadline = time.monotonic() + max(
                        app_config.REALTIME_TURN_MAX_SILENCE_S,
                        app_config.REALTIME_RECV_QUEUE_TIMEOUT_S)
                    self.allow_progress_until(interaction_deadline)
                if _deferred_finalize is not None and any(
                    fc.name in {"delegate_to_main", "reject_turn", "end_conversation"}
                    for fc in message.tool_call.function_calls
                ):
                    # Routing tools finish the consumer turn. Auxiliary tools
                    # (such as emotion) must not hide a subsequent delegate.
                    _log_timing("deferred_wait_expired")
                    await _deferred_finalize()
                    return

            if interaction_status == "IDLE":
                # Process any co-delivered routing tool before closing. IDLE
                # ends execution, not proof of semantic correctness. The
                # legacy classifier is only for sessions without this signal.
                _progress_until = 0.0
                if (_empty_terminal_pending and not _routing_received
                        and not execution_interrupted and not _continuation_overflow
                        and not self._pending_tool_calls):
                    for event in _continuation:
                        self._recv_queue.put(event)
                    _spoken_response += _continuation_text
                    _continuation.clear()
                    _continuation_text = ""
                    _continuation_bytes = 0
                await _finalize_turn_complete(False)
                return

            if message.session_resumption_update:
                update = message.session_resumption_update
                if update.new_handle:
                    self._resumption_handle = update.new_handle

            if message.go_away:
                logger.warning(
                    "Server go_away (time_left=%s)", message.go_away.time_left
                )
                raise ConnectionClosed(None, None)

    # --- Reconnect ---

    def _ensure_connected(self) -> None:
        """Reconnect if not connected. Throttled by an exponential backoff that grows
        on consecutive failures (reset to base on success)."""
        if self._stop_event.is_set():
            return
        if self._connected.is_set():
            return
        now: float = time.monotonic()
        if now - self._last_reconnect_at < self._reconnect_backoff:
            return
        self._last_reconnect_at = now
        self._reconnect()

    @override
    def end_turn(self) -> None:
        """Release the next turn's commit gate when a turn ends without a
        `turn_complete` (e.g. the model ended on a delegate tool call). The recv
        loop is unaffected — it keeps reading this turn in the background until the
        real turn_complete / timeout; any late output is flushed next turn."""
        self._turn_done.set()

    @override
    def force_reconnect(self) -> None:
        """Recover a zombie session: close the live session so the recv loop —
        which may be blocked in `async for session.receive()` on a dead socket —
        unblocks and rebuilds a fresh session via _ensure_connected. Resets the
        reconnect throttle so reconnect happens immediately, not after
        reconnect_delay_s."""
        logger.warning("[realtime] Forcing reconnect — session looks zombie (silent)")
        self._connected.clear()
        self._activity_started = False
        # A fresh session inherits no tool calls — dropping the gate here keeps a
        # call that died with the old socket from muting the new one.
        self._clear_pending_tool_calls()
        self._requires_fresh_session = False
        self._turn_done.set()  # unblock any waiting commit
        self._last_reconnect_at = 0.0  # bypass _ensure_connected throttle
        self._reconnect_backoff = self._reconnect_delay_s  # zombie recovery → retry now
        if self._loop is not None:
            try:
                self._submit_and_wait(self._async_disconnect(), timeout=10.0)
            except Exception as e:
                logger.warning("[realtime] force_reconnect disconnect failed: %s", e)
        self._session = None
        # The recv/send loops' _ensure_connected now rebuilds a fresh session.

    def _reconnect(self) -> None:
        # A worker can observe a transport-close exception during teardown. It
        # belongs to the old agent and must never revive it after its loop has
        # been released.
        if self._stop_event.is_set():
            return
        self._connected.clear()
        self._activity_started = False
        # A fresh session inherits no tool calls — dropping the gate here keeps a
        # call that died with the old socket from muting the new one.
        self._clear_pending_tool_calls()
        self._requires_fresh_session = False
        self._turn_done.set()  # unblock any waiting commit
        if self._loop is None:
            logger.error("[realtime] Cannot reconnect — event loop is None")
            return
        try:
            logger.info("[realtime] Reconnecting...")
            self._submit_and_wait(self._async_disconnect())
            self._submit_and_wait(self._async_connect())
            self._connected.set()
            self._reconnect_backoff = self._reconnect_delay_s  # success → reset backoff
        except Exception as e:
            # Grow backoff so a persistent failure (e.g. usage-limit 4029) doesn't
            # hammer the endpoint every base delay.
            self._reconnect_backoff = min(
                self._reconnect_backoff * 2, self._reconnect_backoff_max
            )
            logger.warning(
                "[realtime] Reconnect failed: %s — next retry in ~%.0fs",
                e, self._reconnect_backoff,
            )

    def _fail_fast_turn(self, reason: str) -> None:
        """End the current turn immediately on a non-idle recv error.

        Without this, any backend failure (proxy go_away, quota / resource
        exhausted, unexpected WS close — anything that is NOT a benign idle
        close 1000) leaves the consumer blocked in receive() for the full
        REALTIME_RECV_QUEUE_TIMEOUT_S before the turn falls back to the main
        agent. Pushing a TurnDoneEvent unblocks receive() now so the main agent
        answers without the dead-air wait. Only fires while a turn is actually
        awaiting output (_turn_done clear); a stray sentinel between turns would
        just be dropped by flush_output() at the next turn start anyway. Late
        output from a successful reconnect is likewise flushed next turn.
        """
        if self._turn_done.is_set():
            return  # no turn awaiting output — nothing to unblock
        self._first_audio_received = False
        self._turn_done.set()
        model = getattr(getattr(self, "_config", None), "model", "") or ""
        self._recv_queue.put(TurnDoneEvent(
            fallback_to_main="extended-thinking" in model,
            user_transcript=getattr(self, "_user_transcript", ""),
            user_turn_id=getattr(self, "_live_user_turn_id", ""),
        ))
        logger.info(
            "[realtime] Recv error (%s) — ending turn now, falling back to main "
            "(skipping receive timeout wait)",
            reason,
        )

    # --- VoiceAgentBase implementation ---

    def _submit_and_wait(self, coro: Any, timeout: float = 30.0) -> Any:
        """Submit a coroutine to the IO thread's loop and block until done."""
        if self._loop is None:
            raise RuntimeError("Event loop is None")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(
            timeout=timeout
        )

    @override
    def _do_connect(self) -> None:
        """Spawn IO thread with event loop, connect on it. Blocks until ready."""
        self._loop = asyncio.new_event_loop()
        self._io_thread = threading.Thread(
            target=self._run_io_loop,
            args=(self._loop,),
            daemon=True,
            name="gemini-io",
        )
        self._io_thread.start()
        try:
            self._submit_and_wait(self._async_connect())
        except Exception:
            # Connection setup happens after the loop/thread are already live.
            # Roll both back on a failed handshake (e.g. proxy WS 1011) instead
            # of leaking an idle gemini-io thread and event loop.
            logger.warning("[realtime] Gemini connect failed — cleaning up IO loop")
            self._do_disconnect()
            raise

    @override
    def _do_disconnect(self) -> None:
        if self._loop is None:
            return
        try:
            # A bounded graceful close normally wakes receive() immediately.
            # If a proxy/SDK close stalls, finally cancels every loop task and
            # stops the owner thread rather than waiting for recv_timeout_s.
            self._submit_and_wait(
                self._async_disconnect(), timeout=self._join_timeout_s
            )
        except Exception as e:
            logger.warning("[realtime] Gemini graceful disconnect failed: %s", e)
        finally:
            self._session = None
            self._exit_stack = None
            self._stop_io_loop()

    @override
    def _send_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._loop is None:
                break
            try:
                event: AgentInputEvent = self._send_queue.get(
                    timeout=self._queue_poll_s
                )
            except queue.Empty:
                continue

            for attempt in range(self._max_retries):
                session = (
                    event.session
                    if isinstance(event, (BoundAudioInputEvent, BoundAudioCommitEvent, AudioStreamEndEvent))
                    else None
                )
                if session is not None and (
                    not self.available or self.audio_session is not session
                ):
                    if isinstance(event, BoundAudioCommitEvent):
                        self._recv_queue.put(TurnDoneEvent())
                    break
                self._ensure_connected()
                if not self._connected.is_set():
                    if session is not None:
                        if isinstance(event, BoundAudioCommitEvent):
                            self._recv_queue.put(TurnDoneEvent())
                        break
                    logger.debug("[realtime] Not connected, skipping attempt %d/%d", attempt + 1, self._max_retries)
                    continue
                try:
                    if isinstance(event, AudioStreamEndEvent):
                        self._submit_and_wait(
                            self._async_end_audio_stream(session), timeout=self._send_timeout_s,
                        )
                    elif isinstance(event, AudioCommitEvent):
                        if not self._turn_done.wait(timeout=10.0):
                            logger.warning("[realtime] Timed out waiting for turn to finish — forcing commit")
                        self._submit_and_wait(
                            self._async_commit(event.queued_at, session=session), timeout=self._send_timeout_s
                        )
                    elif isinstance(event, InputEvent) and event.input is not None:
                        self._submit_and_wait(
                            self._async_send_input(event.input, session=session),
                            timeout=self._send_timeout_s,
                        )
                    break  # Success
                except AudioTurnSessionChanged:
                    # A reconnect cannot retry only the tail of captured speech.
                    # Wake the bound consumer; it owns the complete replay buffer.
                    if isinstance(event, BoundAudioCommitEvent):
                        self._recv_queue.put(TurnDoneEvent())
                    break
                except (ConnectionClosed, genai_errors.APIError) as e:
                    if self._stop_event.is_set():
                        break
                    logger.exception("[realtime] Send failed (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._reconnect()
                    if session is not None:
                        if isinstance(event, BoundAudioCommitEvent):
                            self._recv_queue.put(TurnDoneEvent())
                        break
                except Exception as e:
                    if self._stop_event.is_set():
                        break
                    logger.exception("[realtime] Send error (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._reconnect()
                    if session is not None:
                        if isinstance(event, BoundAudioCommitEvent):
                            self._recv_queue.put(TurnDoneEvent())
                        break

    @override
    def _recv_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._loop is None:
                break
            if not self._connected.is_set():
                # Proactively reconnect (throttled by reconnect_delay_s) so the
                # session SELF-HEALS while disconnected even with NO audio flowing.
                # Critical after a usage-limit (4029) close: voice_service stops
                # feeding audio (orchestrator.available=False once disconnected), so
                # the send loop never triggers a reconnect — without this the session
                # would stay dead forever even after the limit is lifted.
                self._ensure_connected()
                if not self._connected.is_set():
                    _ = self._connected.wait(timeout=self._queue_poll_s)
                continue

            for attempt in range(self._max_retries):
                self._ensure_connected()
                if not self._connected.is_set():
                    logger.debug("[realtime] Not connected, skipping attempt %d/%d", attempt + 1, self._max_retries)
                    continue
                try:
                    self._submit_and_wait(
                        self._async_receive_turn(), timeout=self._recv_timeout_s
                    )
                    break  # Success — turn received
                except ConnectionClosed as e:
                    if self._stop_event.is_set():
                        break
                    code: int | None = getattr(getattr(e, "rcvd", None), "code", None)
                    if code == 1000:
                        logger.info("[realtime] Session closed normally (idle) — reconnecting")
                    else:
                        logger.warning("[realtime] Recv failed (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    # Fail-fast on ANY close, including a benign idle-1000: if it
                    # lands mid-turn the fresh session has no committed audio and
                    # will never answer, so end the turn now instead of waiting out
                    # the receive timeout. No-op when genuinely idle between turns
                    # (_turn_done already set).
                    self._fail_fast_turn(f"ws close {code}")
                    self._connected.clear()
                    self._session = None
                except genai_errors.APIError as e:
                    if self._stop_event.is_set():
                        break
                    # The genai SDK surfaces a normal WS close (code 1000 — idle /
                    # session-duration timeout) as an APIError whose str is
                    # "1000 None", not as ConnectionClosed. Mirror the 1000 special
                    # case in the ConnectionClosed branch above: log at INFO so a
                    # benign idle-reconnect doesn't show up as a red WARNING.
                    code_str: str = str(e).split(" ", 1)[0]
                    if code_str == "1000":
                        logger.info("[realtime] Session closed normally (idle) — reconnecting")
                    else:
                        logger.warning("[realtime] Recv API error (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    # Fail-fast on ANY close (see ConnectionClosed branch): a mid-turn
                    # idle-1000 loses the turn on the fresh session. No-op when idle.
                    self._fail_fast_turn(f"api {code_str}")
                    self._connected.clear()
                    self._session = None
                except Exception as e:
                    if self._stop_event.is_set():
                        break
                    logger.exception("[realtime] Unexpected recv error (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._fail_fast_turn("unexpected")
                    self._connected.clear()
                    self._session = None
