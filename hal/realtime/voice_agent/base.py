"""Abstract base class for realtime voice agents — sync, queue-based."""

import logging
import queue
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Generator
from typing import Any

import numpy as np
import numpy.typing as npt

from hal import config as app_config
from hal.realtime.models import (
    AgentInputEvent,
    AgentOutputEvent,
    AnnounceInput,
    AudioCommitEvent,
    AudioInput,
    InputBase,
    InputEvent,
    OutputBase,
    OutputEvent,
    TurnDoneEvent,
    UserSpeechOutput,
    ExecutionOutput,
    InterruptedOutput,
    MainAgentFallbackOutput,
)

logger = logging.getLogger(__name__)


class AudioTurnSessionChanged(RuntimeError):
    """Captured audio must be replayed in full after its session was replaced."""


class BoundAudioInputEvent(InputEvent):
    """Audio owned by one provider transport, including time spent queued."""

    session: Any


class BoundAudioCommitEvent(AudioCommitEvent):
    """Commit owned by the same transport as the preceding audio frames."""

    session: Any


class VoiceAgentBase(ABC):
    """Sync interface for a realtime voice agent.

    Lifecycle: connect → (append_audio / commit_audio / send + receive) → disconnect.

    Internally each provider runs two threads:
      - _send_loop: drains _send_queue → API (reconnects on error)
      - _recv_loop: API → _recv_queue (reconnects on error)

    Public methods are non-blocking (queue puts) except connect/disconnect/receive.
    """

    def __init__(self, tools: list[dict[str, Any]] | None = None):
        self._tools: list[dict[str, Any]] = tools or []
        self._send_queue: queue.Queue[AgentInputEvent] = queue.Queue()
        self._recv_queue: queue.Queue[AgentOutputEvent] = queue.Queue()
        self._connected = threading.Event()
        self._stop_event = threading.Event()
        self._send_thread: threading.Thread | None = None
        self._recv_thread: threading.Thread | None = None
        # Armed by skip_next_turn_done() (look replay): swallow ONE stale
        # TurnDoneEvent that arrives before any real output. See receive().
        self._newest_output_gen: int = 0
        self._skip_stale_turn_done: bool = False
        self.execution_completed: bool = False
        self.execution_turn_id: str = ""
        # Per-turn override of the receive() silent-turn watchdog. Set by the
        # orchestrator on `look` turns: Gemini's forced thinking over a
        # text-dense image can stay silent >8s right before the answer
        # (device-observed 2026-07-06 — watchdog killed the turn seconds
        # after express_emotion fired). Cleared when receive() exits.
        self._recv_timeout_override_s: float | None = None
        # monotonic() of the last message the SERVER sent us, whatever it was.
        # Liveness, not output — see note_server_activity / receive().
        self._last_server_msg_at: float = 0.0
        self._committed_at: float = 0.0
        self._progress_deadline_at: float = 0.0
        self._output_deadline_at: float = 0.0
        self._progress_watchdog_enabled: bool = False

    @property
    def available(self) -> bool:
        """Whether the agent is connected and ready."""
        return self._connected.is_set()

    @property
    def audio_session(self) -> object:
        """Identity pinned by a turn; reconnecting providers override this."""
        return self

    def validate_audio_session(self, session: object) -> None:
        if not self.available or self.audio_session is not session:
            raise AudioTurnSessionChanged("Audio turn provider session changed")

    @property
    def requires_fresh_session(self) -> bool:
        """Whether the provider session must be replaced before another turn.

        Providers normally return ``False``. Gemini overrides this after an
        unresolved function call because its Live API refuses further client
        input on that session.
        """
        return False

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Sample rate expected by this provider (Hz) — the INPUT/mic rate."""

    @property
    def output_sample_rate(self) -> int:
        """Sample rate of the model's OWN audio output (Hz), for native playback.
        Defaults to the input rate; providers override when they differ."""
        return self.sample_rate

    def connect(self) -> None:
        """Connect to the provider and start send/recv loops."""
        self._stop_event.clear()
        self._do_connect()
        self._connected.set()
        self._send_thread = threading.Thread(
            target=self._send_loop, daemon=True, name="rt-send",
        )
        self._recv_thread = threading.Thread(
            target=self._recv_loop, daemon=True, name="rt-recv",
        )
        self._send_thread.start()
        self._recv_thread.start()

    def disconnect(self) -> None:
        """Stop loops and disconnect."""
        self._stop_event.set()
        self._connected.clear()
        # Close the provider transport before joining worker threads. A receive
        # worker may be blocked in a long provider read; waiting for it first
        # delays the very close that would wake it up and can leave it behind
        # during an agent rebuild.
        try:
            self._do_disconnect()
        finally:
            if self._send_thread is not None:
                self._send_thread.join(timeout=5)
                self._send_thread = None
            if self._recv_thread is not None:
                self._recv_thread.join(timeout=5)
                self._recv_thread = None

    def append_audio(self, audio: npt.NDArray[np.float32], *, session: object | None = None) -> None:
        """Queue a single audio frame for sending (non-blocking)."""
        if session is not None:
            self.validate_audio_session(session)
            self._send_queue.put(BoundAudioInputEvent(input=AudioInput(audio=audio), session=session))
            return
        if self.available:
            self._send_queue.put(InputEvent(input=AudioInput(audio=audio)))

    def end_audio_stream(self, *, session: object) -> bool:
        """Providers opt in to ending a paused live uplink without committing a turn."""
        return False

    def commit_audio(self, *, session: object | None = None) -> None:
        """Queue a commit signal (non-blocking)."""
        if session is not None:
            self.validate_audio_session(session)
        if self.available:
            self._committed_at = time.monotonic()
            self._progress_deadline_at = 0.0
            self._output_deadline_at = 0.0
            logger.info("[realtime][timing] audio_commit_queued gen=%s", getattr(self, "_turn_gen", 0))
            event = AudioCommitEvent() if session is None else BoundAudioCommitEvent(session=session)
            self._send_queue.put(event)

    def allow_progress_until(self, deadline: float) -> None:
        """Let verified provider work survive the receive gap until a deadline.

        Callers supply an absolute, turn-scoped deadline. Repeated search or
        tool updates must not buy a fresh timeout on every message.
        """
        self._progress_deadline_at = deadline

    def allow_output_until(self, deadline: float) -> None:
        """Keep actual buffered answer chunks alive, not generic heartbeats."""
        self._output_deadline_at = deadline

    def flush_output(self) -> None:
        """Drop any output events left on the recv queue from a previous turn.

        Provider responses land on `_recv_queue` asynchronously and can lag the
        caller's local-VAD turn cadence. If the queue is not cleared, the next
        turn's `receive()` reads a STALE prior response (read in milliseconds,
        well before this turn's real reply) and speaks it — the "agent talks on
        its own after a noise blip" + double-reply bug. Call right before
        `commit_audio()` so every turn starts from an empty queue and only ever
        reads its own response.
        """
        dropped = 0
        while True:
            try:
                self._recv_queue.get_nowait()
            except queue.Empty:
                break
            dropped += 1
        if dropped:
            logger.info(
                "[realtime] Flushed %d stale output event(s) before new turn", dropped
            )

    def send(self, inputs: list[InputBase]) -> None:
        """Queue inputs for sending (non-blocking)."""
        if self.available:
            for inp in inputs:
                self._send_queue.put(InputEvent(input=inp))

    @property
    def supports_announce(self) -> bool:
        """Whether this provider can speak a device-initiated AnnounceInput.

        False by default: the caller then renders the announcement without the
        realtime model (text summarizer + TTS). Providers opt in only where a
        text input can open a response of its own on the current wire.
        """
        return False

    def announce(self, text: str) -> bool:
        """Queue a device-initiated spoken reply; read it with receive().

        Returns False when the provider cannot announce right now. The caller
        must flush_output() first so receive() reads only this response.
        """
        if not self.supports_announce or not self.available:
            return False
        self._committed_at = time.monotonic()
        self._progress_deadline_at = 0.0
        self._output_deadline_at = 0.0
        self._send_queue.put(InputEvent(input=AnnounceInput(text=text)))
        return True

    def end_turn(self) -> None:
        """Mark the current turn finished from the consumer's side.

        Default no-op. Gemini's manual-VAD path gates the NEXT turn's commit on a
        `_turn_done` event that only the model's `turn_complete` sets. When a turn
        ends with a tool call (e.g. delegate_to_main) the model sends no
        turn_complete, so without this the next commit blocks on `_turn_done.wait`
        for the full 10s timeout. The orchestrator calls this after a delegate so
        the following turn commits immediately.
        """

    def skip_next_turn_done(self) -> None:
        """Arm receive() to swallow ONE TurnDoneEvent that arrives before any
        real output.

        Look replay: the orchestrator cancels the model's tool-call turn by
        re-committing the user's audio, but the server's turn_complete for the
        CANCELLED turn lands on the recv queue a few hundred ms later — after
        flush_output() already ran for the replay — and receive() would break
        on it, ending the replayed turn empty (device-observed: replay died
        ~340ms after commit). The flag disarms on the first real output, so a
        turn_complete that follows actual content (the live turn's own) is
        never swallowed.
        """
        self._skip_stale_turn_done = True

    def note_server_activity(self) -> None:
        """Record that the server just sent us something, output or not.

        The silent-turn watchdog in receive() cannot tell a model that decided
        not to answer from one that is busy — grounding a search, thinking —
        because both look identical from the queue: nothing arrives. They are
        NOT identical on the wire, though. A working turn keeps producing
        messages we deliberately drop before the queue (thought parts, grounding
        metadata, usage-only frames), while a turn the model abandoned goes
        completely quiet. Providers call this on every inbound message so the
        watchdog can use that difference instead of guessing.
        """
        self._last_server_msg_at = time.monotonic()

    def extend_recv_timeout(self, seconds: float) -> None:
        """Raise the silent-turn watchdog for the CURRENT turn only.

        Look turns: the model may think silently well past the default gap
        window while reading a text-dense image; the orchestrator calls this
        when a `look` fires so receive() waits longer before declaring the
        model silent. Reset automatically when receive() exits.
        """
        self._recv_timeout_override_s = seconds

    def receive(
        self, *, stop_on_done: bool = True, stop_event: threading.Event | None = None,
    ) -> Generator[OutputBase, None, None]:
        """Sync generator — yields OutputBase items from _recv_queue.

        When stop_on_done=True (default), stops at the first TurnDoneEvent.
        When stop_on_done=False, skips TurnDoneEvents and keeps yielding across turns.
        stop_event is checked at most every 100 ms of queue wait; cancellation
        does not complete a turn.
        """
        # Tracks whether this generator ran to a NORMAL end (turn done or
        # silence timeout). A look replay abandons the generator mid-turn
        # (GeneratorExit) — the extended watchdog must survive into the
        # replayed turn, so only a normal end clears the override.
        self.execution_completed = False
        self.execution_turn_id = ""
        turn_ended = False
        stale = 0  # outputs skipped as belonging to a superseded generation
        turn_started: float = time.monotonic()
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return
                recv_timeout: float = (
                    self._recv_timeout_override_s
                    or app_config.REALTIME_RECV_QUEUE_TIMEOUT_S
                )
                progress_remaining = max(
                    getattr(self, "_progress_deadline_at", 0.0),
                    getattr(self, "_output_deadline_at", 0.0),
                ) - time.monotonic()
                # Leave a small scheduling margin for the provider to enqueue
                # its bounded fallback (including handoff context) at expiry.
                queue_timeout = min(recv_timeout, progress_remaining + 0.1) if progress_remaining > 0 else recv_timeout
                try:
                    if stop_event is None:
                        event = self._recv_queue.get(timeout=queue_timeout)
                    else:
                        # Poll only cancellation. Keep the original queue-wait
                        # deadline so each poll is not a new receive timeout.
                        deadline = time.monotonic() + queue_timeout
                        while True:
                            if stop_event.is_set():
                                return
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise queue.Empty
                            try:
                                event = self._recv_queue.get(timeout=min(0.1, remaining))
                                break
                            except queue.Empty:
                                if stop_event.is_set():
                                    return
                                if time.monotonic() >= deadline:
                                    raise
                except queue.Empty:
                    if time.monotonic() < max(
                        getattr(self, "_progress_deadline_at", 0.0),
                        getattr(self, "_output_deadline_at", 0.0),
                    ):
                        continue
                    # No OUTPUT within the gap window. Usually the model staying
                    # silent on a noise / non-directed turn, which is correct —
                    # end quietly and let the main agent have it.
                    #
                    # But a turn can also be silent because it is WORKING: a
                    # Google Search grounding produces no output until the search
                    # returns, and killing it here throws away an answer the
                    # model was about to give and forwards the turn to the main
                    # agent, which is far slower (and the abandoned search is
                    # still billed, and its chunks still land in the session
                    # context). Tell the two apart by liveness on the wire: a
                    # working turn keeps sending messages we drop before the
                    # queue, a silent one sends nothing at all.
                    since_msg: float = time.monotonic() - self._last_server_msg_at
                    waited: float = time.monotonic() - turn_started
                    if (
                        not getattr(self, "_progress_watchdog_enabled", False)
                        and
                        self._last_server_msg_at > 0
                        and since_msg < recv_timeout
                        and waited < app_config.REALTIME_TURN_MAX_SILENCE_S
                    ):
                        logger.info(
                            "receive() no output for %.1fs but the server is still "
                            "talking (last message %.1fs ago, %.1fs into the turn) "
                            "— keeping the turn alive",
                            recv_timeout, since_msg, waited,
                        )
                        continue
                    logger.info(
                        "receive() got no output within %.1fs — ending turn "
                        "(model stayed silent; last server message %.1fs ago)",
                        recv_timeout,
                        since_msg if self._last_server_msg_at > 0 else -1.0,
                    )
                    now = time.monotonic()
                    # Enqueuing capture is not proof that the transport sent it.
                    # Expose bounded metadata to distinguish a stuck sender/tool
                    # gate from a connected provider that has stopped replying.
                    sent_at = getattr(self, "_last_audio_sent_at", None)
                    sender = getattr(self, "_send_thread", None)
                    send_queue = getattr(self, "_send_queue", None)
                    logger.info(
                        "[realtime][transport] agent=%x connected=%s sender_alive=%s "
                        "queued=%s last_audio_sent_s=%.3f pending_tools=%d gated_frames=%d",
                        id(self), self.available,
                        bool(sender and sender.is_alive()),
                        send_queue.qsize() if send_queue is not None else -1,
                        now - sent_at if sent_at is not None else -1.0,
                        len(getattr(self, "_pending_tool_calls", ())),
                        getattr(self, "_gated_audio_frames", 0),
                    )
                    logger.info(
                        "[realtime][timing] receive_timeout gen=%s since_latest_commit_s=%.3f "
                        "progress_seen=%s progress_remaining_s=%.3f output_remaining_s=%.3f",
                        getattr(self, "_turn_gen", 0),
                        now - self._committed_at if self._committed_at else -1.0,
                        bool(self._progress_deadline_at),
                        max(0.0, self._progress_deadline_at - now),
                        max(0.0, self._output_deadline_at - now),
                    )
                    turn_ended = True
                    break
                if isinstance(event, TurnDoneEvent):
                    if self._skip_stale_turn_done:
                        self._skip_stale_turn_done = False
                        logger.info(
                            "[realtime] swallowed stale turn_complete from a cancelled turn"
                        )
                        continue
                    if event.fallback_to_main:
                        # Spoken acknowledgement is not evidence the task ran.
                        self.execution_completed = False
                        self.execution_turn_id = event.user_turn_id
                        turn_ended = stop_on_done
                        yield MainAgentFallbackOutput(
                            transcript=event.user_transcript,
                            handoff_context=event.handoff_context,
                            user_turn_id=event.user_turn_id,
                        )
                        if stop_on_done:
                            break
                        continue
                    if stop_on_done:
                        self.execution_completed = event.execution_completed
                        self.execution_turn_id = event.user_turn_id
                        turn_ended = True
                        break
                    continue
                if isinstance(event, OutputEvent):
                    if isinstance(event.output, (UserSpeechOutput, ExecutionOutput)) or (
                        isinstance(event.output, InterruptedOutput)
                        and event.output.reason == "server_interrupt"
                    ):
                        # Input metadata survives cancelled response generations
                        # and does not make a stale response terminal current.
                        yield event.output
                        continue
                    # Drop output belonging to a superseded generation. The
                    # provider bumps OutputEvent.gen at every turn boundary,
                    # including an interruption, so anything still queued from
                    # a cancelled reply is older than what we have already
                    # yielded and must never reach the speaker. Filtered HERE
                    # rather than in each consumer so every caller — turn path
                    # and live pump alike — gets it for free; providers that
                    # never set gen leave it 0 and nothing is ever dropped.
                    if event.gen < getattr(self, "_newest_output_gen", 0):
                        stale += 1
                        continue
                    if event.gen > getattr(self, "_newest_output_gen", 0):
                        if stale:
                            logger.info(
                                "[realtime] dropped %d output(s) from generation "
                                "%d, superseded by %d",
                                stale, getattr(self, "_newest_output_gen", 0), event.gen,
                            )
                            stale = 0
                        self._newest_output_gen = event.gen
                    self._skip_stale_turn_done = False  # real output → next done is live
                    yield event.output
        finally:
            # Clear the per-turn watchdog override only on a NORMAL turn end;
            # an abandoned generator (look replay re-commit) keeps it for the
            # replayed turn.
            if turn_ended:
                self._recv_timeout_override_s = None
                self._progress_deadline_at = 0.0
                self._output_deadline_at = 0.0

    # --- Abstract: provider-specific implementation ---

    @abstractmethod
    def _do_connect(self) -> None:
        """Establish the WebSocket/API connection. Called from connect()."""

    @abstractmethod
    def _do_disconnect(self) -> None:
        """Close the WebSocket/API connection. Called from disconnect()."""

    @abstractmethod
    def _send_loop(self) -> None:
        """Thread target: drain _send_queue, send to API. Reconnect on error."""

    @abstractmethod
    def _recv_loop(self) -> None:
        """Thread target: read from API, put on _recv_queue. Reconnect on error."""

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False
