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
from hal import cpu_affinity
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
    TextSegmentEndOutput,
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
    """Sync interface for a realtime voice agent (send/recv threads; public methods are non-blocking)."""

    def __init__(self, tools: list[dict[str, Any]] | None = None):
        self._tools: list[dict[str, Any]] = tools or []
        self._send_queue: queue.Queue[AgentInputEvent] = queue.Queue()
        self._recv_queue: queue.Queue[AgentOutputEvent] = queue.Queue()
        self._connected = threading.Event()
        self._stop_event = threading.Event()
        self._send_thread: threading.Thread | None = None
        self._recv_thread: threading.Thread | None = None
        # Armed by skip_next_turn_done() (look replay): swallow one stale TurnDoneEvent.
        self._newest_output_gen: int = 0
        self._skip_stale_turn_done: bool = False
        self.execution_completed: bool = False
        self.execution_turn_id: str = ""
        # Per-turn watchdog override for `look` turns (Gemini can think silently >8s); cleared on exit.
        self._recv_timeout_override_s: float | None = None
        # Liveness, not output (see note_server_activity).
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
        """Whether the provider session must be replaced before another turn (Gemini after an unresolved call)."""
        return False

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Sample rate expected by this provider (Hz) — the INPUT/mic rate."""

    @property
    def output_sample_rate(self) -> int:
        """Sample rate of the model's own audio output (Hz); defaults to the input rate."""
        return self.sample_rate

    def connect(self) -> None:
        """Connect to the provider and start send/recv loops."""
        self._stop_event.clear()
        self._do_connect()
        self._connected.set()
        self._send_thread = threading.Thread(
            target=cpu_affinity.on_cores(cpu_affinity.FAST, self._send_loop),
            daemon=True, name="rt-send",
        )
        self._recv_thread = threading.Thread(
            target=cpu_affinity.on_cores(cpu_affinity.FAST, self._recv_loop),
            daemon=True, name="rt-recv",
        )
        self._send_thread.start()
        self._recv_thread.start()

    def disconnect(self) -> None:
        """Stop loops and disconnect."""
        self._stop_event.set()
        self._connected.clear()
        # Close the transport before joining threads; a blocked receive only wakes on close.
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
        """Let verified provider work survive the receive gap until an absolute turn-scoped deadline."""
        self._progress_deadline_at = deadline

    def allow_output_until(self, deadline: float) -> None:
        """Keep actual buffered answer chunks alive, not generic heartbeats."""
        self._output_deadline_at = deadline

    def flush_output(self) -> None:
        """Drop output events left from a previous turn; call right before `commit_audio()`."""
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
        """Whether this provider can speak a device-initiated AnnounceInput."""
        return False

    def announce(self, text: str) -> bool:
        """Queue a device-initiated spoken reply (flush_output() first); False if not possible now."""
        if not self.supports_announce or not self.available:
            return False
        self._committed_at = time.monotonic()
        self._progress_deadline_at = 0.0
        self._output_deadline_at = 0.0
        self._send_queue.put(InputEvent(input=AnnounceInput(text=text)))
        return True

    def end_turn(self) -> None:
        """Mark the current turn finished from the consumer's side (no-op by default).

        Gemini sends no turn_complete after a tool call; without this the next commit waits 10s.
        """

    def skip_next_turn_done(self) -> None:
        """Arm receive() to swallow ONE TurnDoneEvent that arrives before any real output (look replay)."""
        self._skip_stale_turn_done = True

    def note_server_activity(self) -> None:
        """Record that the server sent something, so the watchdog can tell working turns from silent ones."""
        self._last_server_msg_at = time.monotonic()

    def extend_recv_timeout(self, seconds: float) -> None:
        """Raise the silent-turn watchdog for the current turn only (reset when receive() exits)."""
        self._recv_timeout_override_s = seconds

    def receive(
        self, *, stop_on_done: bool = True, stop_event: threading.Event | None = None,
    ) -> Generator[OutputBase, None, None]:
        """Sync generator yielding OutputBase items from _recv_queue.

        stop_on_done stops at the first TurnDoneEvent; stop_event is polled every 100 ms.
        """
        # Only a normal end clears the override; a look replay abandons the generator mid-turn.
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
                queue_timeout = min(recv_timeout, progress_remaining + 0.1) if progress_remaining > 0 else recv_timeout
                try:
                    if stop_event is None:
                        event = self._recv_queue.get(timeout=queue_timeout)
                    else:
                        # Keep the original deadline so each poll is not a new receive timeout.
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
                    # No output within the gap: silent turn vs working turn (e.g. search grounding),
                    # told apart by liveness on the wire.
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
                    # Enqueued capture is not proof it was sent.
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
                        yield event.output
                        continue
                    # Drop output from a superseded generation (gen bumps at every turn boundary).
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
                    if isinstance(event.output, TextSegmentEndOutput):
                        # Metadata obeys generation ownership but is not real speech.
                        yield event.output
                        continue
                    self._skip_stale_turn_done = False  # real output → next done is live
                    yield event.output
        finally:
            # Clear the override only on a normal turn end (see above).
            if turn_ended:
                self._recv_timeout_override_s = None
                self._progress_deadline_at = 0.0
                self._output_deadline_at = 0.0

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
