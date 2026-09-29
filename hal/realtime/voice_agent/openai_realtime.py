"""OpenAI Realtime (GA API) voice agent: queue-based threading, fully sync.

Contract parity with gemini_live.py: per-response `user_turn_id`, gen bump per response and
interruption, and barge-in truncates the assistant item to what was actually heard.
"""

import base64
import logging
import queue
import threading
import time
from typing import Any, override
from uuid import uuid4

import cv2
import numpy as np
from openai import OpenAI
from openai.resources.realtime.realtime import RealtimeConnection

from hal import config as app_config
from hal.realtime.config import OpenAIConfig
from hal.realtime.enums import OpenAITurnDetectionType
from hal.realtime.exceptions import OpenAIRealtimeError
from hal.realtime.models import (
    AgentInputEvent,
    AudioCommitEvent,
    AudioInput,
    AudioOutput,
    ExecutionOutput,
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
from hal.realtime.utils import (
    base64_pcm16_to_float32,
    float32_to_base64_pcm16,
)
from hal.realtime.voice_agent.base import VoiceAgentBase

logger = logging.getLogger(__name__)
usage_logger = logging.getLogger("hal.realtime.usage.openai")

# USD per 1M tokens (verified 2026-09-16). Substring match IN ORDER: "mini" before full-size,
# "gpt-realtime-2" before "gpt-realtime".
_OPENAI_RATES: tuple[tuple[str, dict[tuple[str, str], float]], ...] = (
    ("mini", {
        ("in", "TEXT"): 0.60, ("in", "AUDIO"): 10.0,
        ("out", "TEXT"): 2.40, ("out", "AUDIO"): 20.0,
        ("cached", "TEXT"): 0.06, ("cached", "AUDIO"): 0.30,
    }),
    ("gpt-realtime-2", {
        ("in", "TEXT"): 4.0, ("in", "AUDIO"): 32.0,
        ("out", "TEXT"): 24.0, ("out", "AUDIO"): 64.0,
        ("cached", "TEXT"): 0.40, ("cached", "AUDIO"): 0.40,
    }),
    ("gpt-realtime", {
        ("in", "TEXT"): 4.0, ("in", "AUDIO"): 32.0,
        ("out", "TEXT"): 16.0, ("out", "AUDIO"): 64.0,
        ("cached", "TEXT"): 0.40, ("cached", "AUDIO"): 0.40,
    }),
)
# Unknown model -> highest text-out table, so cost is a ceiling, not an under-report.
_OPENAI_RATES_FALLBACK: dict[tuple[str, str], float] = max(
    (table for _, table in _OPENAI_RATES), key=lambda r: r[("out", "TEXT")]
)

# "low" sensitivity = higher threshold (API default 0.5); HAL_OPENAI_VAD_THRESHOLD overrides.
_VAD_THRESHOLDS: dict[str, float] = {"low": 0.7, "high": 0.3}

# Errors carrying this event_id (truncate past real length) are benign.
_TRUNCATE_EVENT_ID: str = "hal-truncate"
# Harmless client races, not a broken session.
_BENIGN_ERROR_CODES: frozenset[str] = frozenset({
    "input_audio_buffer_commit_empty",
    "conversation_already_has_active_response",
    "response_cancel_not_active",
})
# Bound on the item_id → user-turn map so a long live session cannot grow it.
_MAX_USER_ITEMS: int = 16


def _openai_rates_for(model: str) -> dict[tuple[str, str], float]:
    """Resolve the per-1M-token rate table for a model (ordered substring match; unknown = ceiling)."""
    for key, table in _OPENAI_RATES:
        if key in model:
            return table
    return _OPENAI_RATES_FALLBACK


class OpenAIRealtimeAgent(VoiceAgentBase):
    """OpenAI Realtime provider.

    end_turn() stays a no-op: response.done arrives even for function-call-only responses.
    """

    def __init__(
        self,
        config: OpenAIConfig,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(tools=tools)
        self._config: OpenAIConfig = config
        self._client: OpenAI = OpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
        )
        self._connection: RealtimeConnection | None = None
        # Reentrant: a send op re-acquires it in _safe_response_create. The blocking recv iteration
        # runs outside the lock on a snapshot so sends aren't starved.
        self._conn_lock: threading.RLock = threading.RLock()
        # Monotonic so NTP cannot skew them.
        self._speech_ended_at: float | None = None
        self._commit_sent_at: float | None = None
        self._first_audio_received: bool = False
        self._reconnect_delay_s: float = config.reconnect_delay_s
        self._max_retries: int = config.max_retries
        self._queue_poll_s: float = config.queue_poll_s
        self._response_wait_s: float = config.response_wait_s
        self._last_reconnect_at: float = 0.0
        self._reconnect_backoff: float = config.reconnect_delay_s
        self._reconnect_backoff_max: float = 60.0
        # Set = model idle; cleared on response.create(), set again on response.done.
        self._turn_done: threading.Event = threading.Event()
        self._turn_done.set()
        # Bumped per response and per interruption so receive() drops superseded audio.
        self._turn_gen: int = 0
        self._live_user_turn_id: str = ""
        self._live_speech_emitted: bool = False
        self._user_transcript: str = ""
        # item_id -> (turn_id, transcript emitted) so a late transcription finds its input turn.
        self._user_items: dict[str, tuple[str, str]] = {}

    @property
    @override
    def sample_rate(self) -> int:
        return self._config.sample_rate

    def _turn_detection(self) -> dict[str, Any] | None:
        """Provider-side VAD settings, or None for manual (client-bracketed) turns (only set knobs are sent)."""
        td_type: OpenAITurnDetectionType | None = self._config.turn_detection_type
        if td_type is None:
            return None
        cfg: dict[str, Any] = {"type": td_type.value}
        if td_type == OpenAITurnDetectionType.SERVER_VAD:
            threshold: float = self._config.vad_threshold or _VAD_THRESHOLDS.get(
                self._config.vad_start_sensitivity, 0.0
            )
            if threshold > 0:
                cfg["threshold"] = threshold
            if self._config.vad_prefix_padding_ms > 0:
                cfg["prefix_padding_ms"] = self._config.vad_prefix_padding_ms
            if self._config.vad_silence_ms > 0:
                cfg["silence_duration_ms"] = self._config.vad_silence_ms
        elif self._config.vad_end_sensitivity in ("low", "high"):
            cfg["eagerness"] = self._config.vad_end_sensitivity
        logger.info(
            "[realtime] server VAD: type=%s threshold=%s prefix_padding=%sms "
            "silence=%sms eagerness=%s",
            cfg["type"],
            cfg.get("threshold", "(provider default)"),
            cfg.get("prefix_padding_ms", "(provider default)"),
            cfg.get("silence_duration_ms", "(provider default)"),
            cfg.get("eagerness", "(provider default)"),
        )
        return cfg

    def _build_session(self) -> dict[str, Any]:
        """The `session.update` payload (GA shape)."""
        audio_input: dict[str, Any] = {
            "format": {"type": "audio/pcm", "rate": self._config.sample_rate},
            # None switches server VAD off for manual turns.
            "turn_detection": self._turn_detection(),
            # Input transcription is the only source of the user's words on this side (API default off).
            "transcription": self._transcription(),
        }
        if self._config.noise_reduction in ("near_field", "far_field"):
            audio_input["noise_reduction"] = {"type": self._config.noise_reduction}

        session: dict[str, Any] = {
            "type": "realtime",
            "instructions": self._config.instructions,
            # Audio only: adding "text" would double-speak the reply.
            "output_modalities": ["audio"],
            "audio": {
                "input": audio_input,
                "output": {
                    "format": {"type": "audio/pcm", "rate": self._config.sample_rate},
                    "voice": self._config.voice.value,
                },
            },
        }

        if self._tools:
            session["tools"] = self._tools
            session["tool_choice"] = "auto"

        if self._config.reasoning_effort is not None:
            session["reasoning"] = {"effort": self._config.reasoning_effort.value}

        truncation_cfg: dict[str, Any] = {"type": self._config.truncation_type.value}
        if self._config.truncation_type.value == "retention_ratio":
            truncation_cfg["retention_ratio"] = self._config.truncation_retention_ratio
        session["truncation"] = truncation_cfg
        return session

    def _transcription(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {"model": self._config.transcribe_model}
        lang: str | None = self._config.language
        if lang:
            # The API wants ISO-639-1; the device setting may be a BCP-47 tag.
            cfg["language"] = lang.split("-", 1)[0].lower()
        return cfg

    def _sync_connect(self) -> None:
        # A reopened session must not inherit input ownership from the old one.
        self._live_user_turn_id = ""
        self._live_speech_emitted = False
        self._user_transcript = ""
        self._user_items = {}
        logger.info(
            "Connecting to OpenAI Realtime API (base_url=%s, model=%s)",
            self._config.base_url,
            self._config.model,
        )

        self._connection = self._client.realtime.connect(
            model=self._config.model,
        ).enter()
        self._connection.session.update(session=self._build_session())
        logger.info("[realtime] OpenAI Realtime session open (voice=%s)", self._config.voice)

    def _sync_disconnect(self) -> None:
        if self._connection is not None:
            logger.info("[realtime] Disconnecting from OpenAI Realtime API")
            self._connection.close()
            self._connection = None

    def _sync_send_input(self, input: InputBase) -> None:
        with self._conn_lock:
            if self._connection is None:
                return

            if isinstance(input, AudioInput):
                b64_audio: str = float32_to_base64_pcm16(input.audio)
                self._connection.input_audio_buffer.append(audio=b64_audio)

            elif isinstance(input, TextInput):
                self._connection.conversation.item.create(
                    item={
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": input.text}],
                    }
                )

            elif isinstance(input, ImageInput):
                _: bool
                buf: np.ndarray
                _, buf = cv2.imencode(".png", input.image)
                b64_img: str = base64.b64encode(buf.tobytes()).decode("ascii")
                data_uri: str = f"data:image/png;base64,{b64_img}"
                self._connection.conversation.item.create(
                    item={
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_image", "image_url": data_uri}],
                    }
                )

            elif isinstance(input, FunctionCallResultInput):
                self._connection.conversation.item.create(
                    item={
                        "type": "function_call_output",
                        "call_id": input.call_id,
                        "output": input.output,
                    }
                )
                # Fire-and-forget tools must not spawn a fresh response (the model would speak twice).
                if input.trigger_response:
                    self._safe_response_create()

    def _sync_commit(self, turn_end_queued_at: float | None = None) -> None:
        with self._conn_lock:
            if self._connection is None:
                return
            if self._config.turn_detection_type is not None:
                # Server VAD commits and creates the response itself; a client commit would collide.
                logger.debug("[realtime] Commit skipped — server VAD brackets the turn")
                return
            self._connection.input_audio_buffer.commit()
            self._commit_sent_at = time.monotonic()
            if turn_end_queued_at is not None:
                logger.info(
                    "[realtime] Turn timing: local_end->commit_sent=%.0fms",
                    (self._commit_sent_at - turn_end_queued_at) * 1000,
                )
            self._safe_response_create()

    def _safe_response_create(self) -> None:
        """Wait for any active response to finish, then create a new one (wait happens outside the lock)."""
        if not self._turn_done.wait(timeout=self._response_wait_s):
            logger.warning("[realtime] Timed out waiting for active response to finish — forcing new response")
        with self._conn_lock:
            if self._connection is None:
                return
            self._turn_done.clear()
            self._speech_ended_at = time.monotonic()
            self._connection.response.create()

    def _observe_user_speech(
        self,
        *,
        turn_id: str | None = None,
        endpoint_at: float | None = None,
        transcript: str = "",
        transcript_finished: bool = False,
    ) -> None:
        """Publish one input key; `turn_id` pins the observation to an earlier input."""
        if not app_config.LIVE_MODE:
            return
        if turn_id is not None and turn_id != getattr(self, "_live_user_turn_id", ""):
            if not turn_id:
                return
            self._recv_queue.put(OutputEvent(
                gen=getattr(self, "_turn_gen", 0),
                output=UserSpeechOutput(
                    turn_id=turn_id,
                    transcript=transcript,
                    transcript_finished=transcript_finished,
                    user_turn_id=turn_id,
                    endpoint_at=endpoint_at,
                    method="server_vad" if endpoint_at is not None else "provider_transcript",
                ),
            ))
            return
        if transcript_finished and not transcript and endpoint_at is None:
            # Completion-only metadata cannot invent an input turn.
            if not getattr(self, "_live_user_turn_id", ""):
                return
        if not getattr(self, "_live_user_turn_id", ""):
            self._live_user_turn_id = "openai-" + uuid4().hex
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
                method="server_vad" if endpoint_at is not None else "provider_transcript",
            ),
        ))

    def _remember_user_item(self, item_id: str | None) -> None:
        """Bind a user audio item to the live turn being captured right now."""
        if not item_id:
            return
        items: dict[str, tuple[str, str]] = getattr(self, "_user_items", None) or {}
        self._user_items = items
        items[item_id] = (getattr(self, "_live_user_turn_id", ""), "")
        while len(items) > _MAX_USER_ITEMS:
            del items[next(iter(items))]

    def _on_input_transcript(self, item_id: str | None, text: str, *, finished: bool) -> None:
        """Route an input transcription chunk to its input turn (`completed` emits only the unstreamed part)."""
        items: dict[str, tuple[str, str]] = getattr(self, "_user_items", None) or {}
        known = items.get(item_id or "")
        turn_id, emitted = known if known is not None else (None, "")
        if finished:
            chunk = text[len(emitted):] if emitted and text.startswith(emitted) else (
                "" if emitted and text == emitted else text
            )
        else:
            chunk = text
        if known is not None and item_id:
            items[item_id] = (turn_id or "", emitted + chunk)
        if chunk:
            logger.info("[realtime] <<< user said: %r", chunk)
            self._user_transcript = getattr(self, "_user_transcript", "") + chunk
        if chunk or finished:
            self._observe_user_speech(
                turn_id=turn_id, transcript=chunk, transcript_finished=finished,
            )

    def _output_rate(self) -> int:
        cfg = getattr(self, "_config", None)
        return cfg.sample_rate if cfg is not None else 24000

    def _handle_interrupt(
        self,
        conn: RealtimeConnection | None,
        *,
        user_turn_id: str,
        item: tuple[str, int] | None,
        received_ms: float,
    ) -> None:
        """The user barged in: drop queued output, announce the interruption, and truncate the server item."""
        dropped = 0
        dropped_ms = 0.0
        metadata: list[OutputEvent] = []
        while True:
            try:
                queued = self._recv_queue.get_nowait()
            except queue.Empty:
                break
            dropped += 1
            if isinstance(queued, OutputEvent):
                if isinstance(queued.output, (UserSpeechOutput, ExecutionOutput)) or (
                    isinstance(queued.output, InterruptedOutput)
                    and queued.output.reason == "server_interrupt"
                ):
                    metadata.append(queued)
                elif isinstance(queued.output, AudioOutput):
                    dropped_ms += len(queued.output.audio) * 1000.0 / self._output_rate()
            elif app_config.LIVE_MODE and isinstance(queued, TurnDoneEvent):
                metadata.append(OutputEvent(
                    gen=getattr(self, "_turn_gen", 0),
                    output=ExecutionOutput(
                        user_turn_id=queued.user_turn_id,
                        execution_completed=queued.execution_completed,
                    ),
                ))
        for queued in metadata:
            self._recv_queue.put(queued)
        # Cancelled-reply output is now older than what follows; receive() drops it by generation.
        self._turn_gen = getattr(self, "_turn_gen", 0) + 1
        if app_config.LIVE_MODE:
            self._recv_queue.put(OutputEvent(
                gen=self._turn_gen,
                output=InterruptedOutput(
                    reason="server_interrupt", at=time.monotonic(),
                    user_turn_id=user_turn_id,
                ),
            ))
        if item is not None and conn is not None:
            # received - queued over-estimates delivered; an overshoot only yields a benign error.
            self._truncate_item(conn, item, max(0, int(received_ms - dropped_ms)))
        self._first_audio_received = False
        self._turn_done.set()
        logger.info(
            "[realtime] Response interrupted — dropped %d queued output(s), gen=%d",
            dropped, self._turn_gen,
        )

    def _truncate_item(self, conn: RealtimeConnection, item: tuple[str, int], audio_end_ms: int) -> None:
        item_id, content_index = item
        try:
            with self._conn_lock:
                if self._connection is not conn:
                    return
                conn.conversation.item.truncate(
                    item_id=item_id,
                    content_index=content_index,
                    audio_end_ms=audio_end_ms,
                    event_id=_TRUNCATE_EVENT_ID,
                )
            logger.debug("[realtime] Truncated %s at %dms", item_id, audio_end_ms)
        except Exception as e:  # a failed truncate must never end the turn
            logger.warning("[realtime] conversation.item.truncate failed: %s", e)

    def _log_usage(self, response: Any) -> None:
        """Log the per-turn token bill to openai_usage.log (cached=0 every turn means session churn)."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        cfg = getattr(self, "_config", None)
        model: str = cfg.model if cfg is not None else ""
        rates = _openai_rates_for(model)
        parts: list[str] = []
        cost = 0.0
        attributed = {"in": 0, "out": 0}
        in_details = getattr(usage, "input_token_details", None)
        out_details = getattr(usage, "output_token_details", None)
        for direction, details in (("in", in_details), ("out", out_details)):
            for key in ("text_tokens", "audio_tokens"):
                tok = getattr(details, key, 0) or 0
                mod = key.split("_", 1)[0].upper()
                attributed[direction] += tok
                c = tok * rates.get((direction, mod), 0.0) / 1_000_000
                cost += c
                parts.append("%s_%s=%d($%.5f)" % (direction, mod.lower(), tok, c))
        # Untagged tokens (image input) are unpriced, so est is a floor.
        unattr_in = (getattr(usage, "input_tokens", 0) or 0) - attributed["in"]
        unattr_out = (getattr(usage, "output_tokens", 0) or 0) - attributed["out"]
        cached = getattr(in_details, "cached_tokens", 0) or 0
        cached_details = getattr(in_details, "cached_tokens_details", None)
        cached_audio = getattr(cached_details, "audio_tokens", 0) or 0
        # Untagged cached tokens are text (the system-instruction floor).
        cached_text = (getattr(cached_details, "text_tokens", None)
                       if cached_details is not None else None)
        if cached_text is None:
            cached_text = max(0, cached - cached_audio)
        saving = (
            cached_text * (rates[("in", "TEXT")] - rates[("cached", "TEXT")])
            + cached_audio * (rates[("in", "AUDIO")] - rates[("cached", "AUDIO")])
        ) / 1_000_000
        usage_logger.info(
            "[realtime] OpenAI usage: model=%s %s +unattr(%din/%dout) | "
            "cached=%dtok total=%dtok est_full>=$%.5f est_cached>=$%.5f",
            model, " ".join(parts) or "-", unattr_in, unattr_out, cached,
            getattr(usage, "total_tokens", 0) or 0, cost, max(0.0, cost - saving),
        )

    def _sync_receive_turn(self, conn: RealtimeConnection) -> bool:
        """Read one full turn from the `conn` snapshot onto _recv_queue.

        Returns True if `response.done` was seen, False on a clean close/recycle mid-turn.
        """
        self._turn_gen = getattr(self, "_turn_gen", 0) + 1
        self._first_audio_received = False
        # Frozen at response.created so a later transcription can't re-own the reply.
        response_user_turn_id: str | None = None
        active_response_id: str | None = None
        cancelled_response_id: str | None = None
        execution_interrupted = False
        active_item: tuple[str, int] | None = None
        received_ms = 0.0
        transcript_chunks = 0

        for event in conn:
            # Liveness for the silent-turn watchdog (see VoiceAgentBase.note_server_activity).
            self.note_server_activity()
            etype: str = getattr(event, "type", "")

            match etype:
                case "input_audio_buffer.speech_started":
                    if active_response_id is not None and not execution_interrupted:
                        # Barge-in: the server cancels the response; flush the local queue now.
                        execution_interrupted = True
                        cancelled_response_id = active_response_id
                        owner = (response_user_turn_id if response_user_turn_id is not None
                                 else getattr(self, "_live_user_turn_id", ""))
                        response_user_turn_id = owner
                        self._handle_interrupt(
                            conn, user_turn_id=owner, item=active_item,
                            received_ms=received_ms,
                        )
                    self._live_user_turn_id = ""
                    self._live_speech_emitted = False
                    self._observe_user_speech()
                    self._remember_user_item(getattr(event, "item_id", None))

                case "input_audio_buffer.speech_stopped":
                    self._speech_ended_at = time.monotonic()
                    self._observe_user_speech(endpoint_at=self._speech_ended_at)

                case "input_audio_buffer.committed":
                    # Manual turns have no speech_started; the user item id becomes known here.
                    item_id = getattr(event, "item_id", None)
                    items: dict[str, tuple[str, str]] = getattr(self, "_user_items", None) or {}
                    if item_id and item_id not in items:
                        self._remember_user_item(item_id)

                case "conversation.item.input_audio_transcription.delta":
                    self._on_input_transcript(
                        getattr(event, "item_id", None), getattr(event, "delta", "") or "",
                        finished=False,
                    )

                case "conversation.item.input_audio_transcription.completed":
                    self._on_input_transcript(
                        getattr(event, "item_id", None),
                        getattr(event, "transcript", "") or "",
                        finished=True,
                    )

                case "conversation.item.input_audio_transcription.failed":
                    logger.warning(
                        "[realtime] Input transcription failed: %s",
                        getattr(event, "error", None),
                    )

                case "response.created":
                    active_response_id = getattr(getattr(event, "response", None), "id", None) or ""
                    if response_user_turn_id is None:
                        response_user_turn_id = getattr(self, "_live_user_turn_id", "")

                case "response.output_audio.delta":
                    if execution_interrupted and getattr(event, "response_id", None) == cancelled_response_id:
                        continue  # in-flight audio of the cancelled reply
                    if response_user_turn_id is None:
                        response_user_turn_id = getattr(self, "_live_user_turn_id", "")
                    audio = base64_pcm16_to_float32(event.delta)
                    received_ms += len(audio) * 1000.0 / self._output_rate()
                    item_id = getattr(event, "item_id", None)
                    if item_id:
                        active_item = (item_id, getattr(event, "content_index", 0) or 0)
                    if not self._first_audio_received:
                        self._first_audio_received = True
                        self._log_first_audio_latency()
                    self._recv_queue.put(
                        OutputEvent(
                            gen=getattr(self, "_turn_gen", 0),
                            output=AudioOutput(
                                user_turn_id=response_user_turn_id or "",
                                audio=audio,
                            ),
                        )
                    )

                case "response.output_audio_transcript.delta":
                    if execution_interrupted and getattr(event, "response_id", None) == cancelled_response_id:
                        continue
                    if response_user_turn_id is None:
                        response_user_turn_id = getattr(self, "_live_user_turn_id", "")
                    if not transcript_chunks:
                        # Reset the live TTS queue so this response never plays behind a stale one.
                        self._recv_queue.put(OutputEvent(
                            gen=getattr(self, "_turn_gen", 0),
                            output=InterruptedOutput(
                                reason="output_reset",
                                user_turn_id=response_user_turn_id or "",
                            ),
                        ))
                    transcript_chunks += 1
                    self._recv_queue.put(
                        OutputEvent(
                            gen=getattr(self, "_turn_gen", 0),
                            output=TextOutput(
                                text=event.delta,
                                user_turn_id=response_user_turn_id or "",
                            ),
                        )
                    )

                case "response.output_text.delta":
                    # Audio-only session: emitting text too would double-speak the reply.
                    continue

                case "response.function_call_arguments.done":
                    if response_user_turn_id is None:
                        response_user_turn_id = getattr(self, "_live_user_turn_id", "")
                    user_transcript = getattr(self, "_user_transcript", "").strip()
                    self._user_transcript = ""
                    logger.debug(
                        "[realtime] Function call: %s (call_id=%s)", event.name, event.call_id
                    )
                    self._recv_queue.put(
                        OutputEvent(
                            gen=getattr(self, "_turn_gen", 0),
                            output=FunctionCallOutput(
                                user_turn_id=response_user_turn_id or "",
                                name=event.name,
                                arguments=event.arguments,
                                call_id=event.call_id,
                                user_transcript=user_transcript,
                            ),
                        )
                    )

                case "response.done":
                    response = getattr(event, "response", None)
                    status = getattr(response, "status", None)
                    logger.debug("[realtime] Response complete (status=%s)", status)
                    self._log_usage(response)
                    if status == "cancelled" and not execution_interrupted:
                        # Cancelled without our speech_started (client cancel or semantic VAD): flush now.
                        execution_interrupted = True
                        owner = (response_user_turn_id if response_user_turn_id is not None
                                 else getattr(self, "_live_user_turn_id", ""))
                        response_user_turn_id = owner
                        self._handle_interrupt(
                            conn, user_turn_id=owner, item=active_item,
                            received_ms=received_ms,
                        )
                    self._first_audio_received = False
                    self._turn_done.set()
                    owner = (response_user_turn_id if response_user_turn_id is not None
                             else getattr(self, "_live_user_turn_id", ""))
                    self._recv_queue.put(TurnDoneEvent(
                        execution_completed=status == "completed" and not execution_interrupted,
                        user_turn_id=owner,
                    ))
                    if response_user_turn_id is None or (
                        getattr(self, "_live_user_turn_id", "") == response_user_turn_id
                    ):
                        # Answered: a later unsolicited reply must not be attributed to this input.
                        self._live_user_turn_id = ""
                        self._live_speech_emitted = False
                        self._user_transcript = ""
                    return True

                case "error":
                    err = getattr(event, "error", None)
                    code = getattr(err, "code", None) or ""
                    if code in _BENIGN_ERROR_CODES or getattr(err, "event_id", None) == _TRUNCATE_EVENT_ID:
                        logger.info("[realtime] Realtime API notice (%s): %s", code, getattr(err, "message", err))
                        continue
                    logger.error("[realtime] Realtime API error: %s", err)
                    raise OpenAIRealtimeError(f"Realtime API error: {err}")

                case _:
                    pass

        # Iteration ended without response.done: connection closed cleanly.
        return False

    def _log_first_audio_latency(self) -> None:
        now = time.monotonic()
        speech_ended_at = getattr(self, "_speech_ended_at", None)
        commit_sent_at = getattr(self, "_commit_sent_at", None)
        if speech_ended_at is not None:
            if commit_sent_at is not None:
                logger.info(
                    "[realtime] Response latency: %.0fms (speech_end->first_audio; "
                    "commit_sent->first_audio=%.0fms)",
                    (now - speech_ended_at) * 1000, (now - commit_sent_at) * 1000,
                )
            else:
                logger.info(
                    "[realtime] Response latency: %.0fms (speech_end->first_audio; server VAD)",
                    (now - speech_ended_at) * 1000,
                )
        self._speech_ended_at = None
        self._commit_sent_at = None

    def _ensure_connected(self) -> None:
        """Reconnect if not connected, throttled by exponential backoff (reset on success)."""
        if self._stop_event.is_set():
            return
        if self._connected.is_set():
            return
        now: float = time.monotonic()
        if now - self._last_reconnect_at < self._reconnect_backoff:
            return
        self._last_reconnect_at = now
        self._reconnect()

    def _reconnect(self) -> None:
        if self._stop_event.is_set():
            return
        with self._conn_lock:
            if self._stop_event.is_set():
                return
            # Another thread may have reconnected while we waited for the lock.
            if self._connected.is_set():
                return
            self._turn_done.set()  # unblock any waiting commit
            try:
                logger.info("[realtime] Reconnecting...")
                self._sync_disconnect()
                self._sync_connect()
                self._connected.set()
                self._reconnect_backoff = self._reconnect_delay_s
            except Exception as e:
                self._reconnect_backoff = min(
                    self._reconnect_backoff * 2, self._reconnect_backoff_max
                )
                logger.warning(
                    "[realtime] Reconnect failed: %s — next retry in ~%.0fs",
                    e, self._reconnect_backoff,
                )

    def _fail_fast_turn(self, reason: str) -> None:
        """End the current turn immediately on a recv error so the main agent answers without dead air.

        Only fires while a turn is awaiting output (_turn_done clear).
        """
        if self._turn_done.is_set():
            return  # no turn awaiting output — nothing to unblock
        self._first_audio_received = False
        self._turn_done.set()
        self._recv_queue.put(TurnDoneEvent())
        logger.info(
            "[realtime] Recv error (%s) — ending turn now, falling back to main "
            "(skipping receive timeout wait)",
            reason,
        )

    def _drop_connection(self, conn: RealtimeConnection | None) -> None:
        """Mark the connection dead only if `conn` is still current (both loops call this)."""
        with self._conn_lock:
            if conn is None or self._connection is conn:
                self._connected.clear()
                self._connection = None

    @override
    def _do_connect(self) -> None:
        self._sync_connect()

    @override
    def _do_disconnect(self) -> None:
        self._sync_disconnect()

    @override
    def _send_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                event: AgentInputEvent = self._send_queue.get(timeout=self._queue_poll_s)
            except queue.Empty:
                continue

            for attempt in range(self._max_retries):
                self._ensure_connected()
                if self._stop_event.is_set():
                    break
                if not self._connected.is_set():
                    logger.debug("[realtime] Not connected, skipping attempt %d/%d", attempt + 1, self._max_retries)
                    continue
                conn: RealtimeConnection | None = self._connection
                try:
                    if isinstance(event, AudioCommitEvent):
                        self._sync_commit(event.queued_at)
                    elif isinstance(event, InputEvent) and event.input is not None:
                        self._sync_send_input(event.input)
                    break
                except Exception as e:
                    if self._stop_event.is_set():
                        break
                    logger.exception("[realtime] Send failed (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._drop_connection(conn)

    @override
    def _recv_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._connected.is_set():
                # Reconnect proactively so the session self-heals with no audio flowing.
                self._ensure_connected()
                if not self._connected.is_set():
                    self._connected.wait(timeout=self._queue_poll_s)
                continue

            for attempt in range(self._max_retries):
                self._ensure_connected()
                if self._stop_event.is_set():
                    break
                with self._conn_lock:
                    conn: RealtimeConnection | None = (
                        self._connection if self._connected.is_set() else None
                    )
                if conn is None:
                    logger.debug("[realtime] Not connected, skipping attempt %d/%d", attempt + 1, self._max_retries)
                    continue
                try:
                    completed: bool = self._sync_receive_turn(conn)
                    if self._stop_event.is_set():
                        break
                    # Fail fast only when the turn ended without response.done; on a normal completion
                    # it would race the next commit and end turn N+1 empty.
                    if not completed:
                        self._fail_fast_turn("connection closed mid-turn")
                    break
                except OpenAIRealtimeError as e:
                    if self._stop_event.is_set():
                        break
                    logger.warning("[realtime] Recv failed (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._fail_fast_turn("api error")
                    self._drop_connection(conn)
                except Exception as e:
                    if self._stop_event.is_set():
                        break
                    logger.exception("[realtime] Unexpected recv error (attempt %d/%d): %s", attempt + 1, self._max_retries, e)
                    self._fail_fast_turn("unexpected")
                    self._drop_connection(conn)
