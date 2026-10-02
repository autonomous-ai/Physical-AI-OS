"""Realtime orchestrator: voice agent lifecycle and turn processing.

Caller flow: append_audio() frames, commit_audio(), then iterate stream_output(),
which yields outputs and stops after a DelegateSignal or RejectSignal.
"""

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

import hal.config as config
import hal.presets as presets
from hal.realtime.config import (
    GeminiConfig,
    GPTLiveConfig,
    OpenAIConfig,
    PipecatV1Config,
    _load_language,
    gemini_needs_idle_workaround,
)
from hal.realtime.enums.gemini import GeminiVoice
from hal.realtime.context_manager import (
    CONTEXT_MANAGERS,
    ContextManagerBase,
    OpenClawContextManager,
)
from hal.realtime.enums import AgentGateway
from hal.realtime.find_intent import is_find_request
from hal.realtime.emotion_markers import EmotionMarkerStream, marker_instructions
from hal.realtime.models import (
    FunctionCallOutput,
    FunctionCallResultInput,
    ImageInput,
    MainAgentFallbackOutput,
    OutputBase,
    TextInput,
)
from hal.realtime.models.signal import (
    DelegateSignal,
    EndCallSignal,
    LookReplaySignal,
    RejectSignal,
)
from hal.realtime.models.output import AudioOutput, TextOutput, TextSegmentEndOutput, ExecutionOutput, InterruptedOutput, UserSpeechOutput
from hal.realtime.summarizer import RealtimeSummarizer
from hal.realtime.voice_agent.base import AudioTurnSessionChanged, VoiceAgentBase


@dataclass(frozen=True)
class AudioTurnBinding:
    """One capture's agent and provider socket; never follows a reconnect."""

    agent: VoiceAgentBase
    session: object


logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE: int = 16000
INITIAL_CONNECT_RETRY_DELAY_S: float = 2.0
INITIAL_CONNECT_RETRY_MAX_DELAY_S: float = 60.0
# Park threshold has tens of seconds of margin, so a coarse poll is enough.
IDLE_PARK_POLL_S: float = 5.0
# Above a normal handshake (~1-2s); bounded so a hung proxy still falls back.
PREWARM_JOIN_TIMEOUT_S: float = 4.0
# Caps how long an abandoned prepare_turn() can suppress parking.
TURN_IN_FLIGHT_MAX_S: float = 120.0
# Past it the user turn goes ahead; flush_output() drops the drain's leftovers.
ANNOUNCE_DRAIN_S: float = 3.0
# The pipecat relay measured 11.6 s to first token on a cold 12.5k-token context.
ANNOUNCE_RECV_TIMEOUT_S: float = 20.0

DELEGATE_TOOL_NAME: str = "delegate_to_main"
DELEGATE_TOOL_DESCRIPTION: str = (
    "Call this for requests requiring the main system: device or desktop actions, "
    "music, scheduling, memory, skills, real-time facts, or other non-conversational work. "
    "Clearly addressed reports of headache, fatigue, dizziness, stuffiness or "
    "difficulty concentrating require main's wellbeing skill, even without a "
    "question or action verb. A trailing 'okay' does not erase a symptom report. "
    "Questions about current room air, CO2, temperature, humidity or ventilation, "
    "and discomfort follow-ups such as 'Could it be because the room is too airtight?' "
    "also delegate for environment context. Do not substitute generic advice, "
    "a cause, a relief promise or a medical disclaimer for delegation. "
    "Hand off immediately without speech, including urgent symptom reports; "
    "main decides whether sensor checks are appropriate. General educational "
    "questions unrelated to current discomfort or room conditions remain direct. "
    "Digital work also requires main even without naming Harness or an agent: "
    "building or editing software, models or CAD parts, engineering simulations, "
    "media or music creation, slides, spreadsheets and data analysis. When DEVICE "
    "IDENTITY identifies Lamp, main prefers connected Harness for digital work. "
    "For fresh tasks before dispatch with confirmed offline/unpaired Harness, main "
    "uses its other available tools unless Harness or a remote target was explicitly "
    "required. Existing remote tasks and uncertain delivery keep their recovery "
    "rules; do not duplicate them. Always delegate digital work to main even when "
    "Harness is unavailable; main decides fallback. Do not choose agents, invent Store "
    "operations or claim app readiness yourself. Music playback and physical "
    "device actions keep their existing skill routes; general knowledge questions "
    "remain direct answers. "
    "Any request naming Harness, a Mac agent, Codex, Claude, a project/worktree/session, "
    "or asking an agent to use a browser must go to the main runtime's harness-use "
    "skill. This includes general web research such as asking agent temp to find "
    "restaurants; do not answer, search, or claim results yourself. Never perform "
    "that agent task on the device. "
    "Finding, locating or looking for a physical object or a person — in ANY "
    "phrasing: 'find my keys', 'where is my cup', 'can you help me find my pen', "
    "'do you see my pen anywhere', 'look around for X', 'where are you' — is a "
    "device action performed with the camera and servos, never a conversation: "
    "delegate it with the user's words. Do not answer with guesses, questions "
    "about what it looks like or where they last had it, offers to look, or "
    "claims about what you can see. "
    "Research, analysis, comparison, brainstorming, planning an idea, and any "
    "request for a report, summary or document also delegate: these are "
    "multi-step work with a delivered document, not a single live fact. "
    "Sending, forwarding or sharing anything through a channel or connector — a "
    "message, a photo or camera capture, a picture from the web, a file, a link, "
    "or a recap of this conversation via Telegram, email, Slack or any other "
    "channel — and any question about whether or where you CAN deliver such "
    "things, also delegate: only the main system holds the channels. Never claim "
    "you can or cannot send, never say it was sent, never look up or describe "
    "the picture instead of sending it. "
    "Clearly heard answers, corrections, and stop requests for a known pending task "
    "also delegate, even without an action verb. Use conversation context to recognize "
    "the task, but forward ONLY the current user's faithfully understood words in "
    "their spoken language. Preserve named apps, agent providers, projects, worktrees, "
    "session references, dictated text verbatim, all clauses, "
    "timing, quantities, and supplied parameters. Do not summarize away details, "
    "translate into English, append commentary, or retell prior tasks; the main agent "
    "already has that conversation. Never invent missing details. "
    "Keep the user's own key words rather than renaming the request into a category: "
    "the main agent routes on vocabulary, so 'show me how far you can move' must "
    "arrive as those words, not as 'movement demonstration'. "
    "ONLY call when you clearly understood a request or task follow-up addressed to "
    "the device. Do not invent requests from unclear or noise-like audio such as "
    "a cough or an unclear syllable; remain silent for background speech."
)

DELEGATE_TOOL: dict[str, Any] = {
    "type": "function",
    "name": DELEGATE_TOOL_NAME,
    "description": DELEGATE_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Only the current user's faithfully understood request or task follow-up, in their spoken language. Preserve named apps, dictated text verbatim, all clauses and supplied parameters. No translation, commentary, summary of earlier turns, or invented details. Must not be empty.",
            },
        },
        "required": ["message"],
    },
}

REJECT_TURN_TOOL_NAME: str = "reject_turn"
REJECT_TURN_TOOL_DESCRIPTION: str = (
    "Explicitly drop this turn when there is nothing to answer or delegate. Call "
    "it in either case: (a) the audio is not addressed to this device — "
    "background noise, other people's conversation, or an overheard request "
    "(still a valid rejection even if you could fulfill it); or (b) it IS "
    "addressed to you but carries no request, question, or action — a bare "
    "acknowledgment (\"okay\", \"yeah\", \"right\", \"one sec\"), filler, or a lone "
    "stray word or garbled fragment (\"football.\", \"reef\"); or (c) it is "
    "clearly spoken in a language this device is not configured for — do not "
    "translate or answer it, reject the turn. Prefer this over "
    "simply going silent for those: a silent turn falls through to the slower "
    "main agent, and delegating a non-request wastes a full main-agent turn that "
    "returns nothing. Never call it for an actual request you could answer or "
    "delegate, and never merely because you are uncertain — an uncertain possible "
    "request must keep its normal fallback. Keep voice output completely blank."
)

REJECT_TURN_TOOL: dict[str, Any] = {
    "type": "function",
    "name": REJECT_TURN_TOOL_NAME,
    "description": REJECT_TURN_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

END_CALL_TOOL_NAME: str = "end_conversation"
END_CALL_TOOL_DESCRIPTION: str = (
    "End the live voice session and return the device to standby. Call this "
    "when the user says goodbye, says they are done, or the conversation has "
    "clearly finished. Say your short farewell in the SAME turn as this call — "
    "the device stays open a few seconds afterwards so it is heard, then hangs "
    "up. Do not call it merely because the user paused, and never call it to "
    "avoid answering something."
)

END_CALL_TOOL: dict[str, Any] = {
    "type": "function",
    "name": END_CALL_TOOL_NAME,
    "description": END_CALL_TOOL_DESCRIPTION,
    "parameters": {"type": "object", "properties": {}},
}

DEFAULT_EMOTION_INTENSITY: float = 0.8

EMOTION_TOOL_NAME: str = "express_emotion"
# Excludes device-driven states (idle, listening, sleepy, scan, nod, music_*).
EMOTION_TOOL_EMOTIONS: list[str] = [
    presets.EMO_HAPPY, presets.EMO_EXCITED, presets.EMO_CURIOUS,
    presets.EMO_THINKING, presets.EMO_CARING, presets.EMO_LAUGH,
    presets.EMO_SHY, presets.EMO_SAD, presets.EMO_SHOCK,
    presets.EMO_CONFUSED, presets.EMO_GREETING, presets.EMO_GOODBYE,
]
EMOTION_TOOL_DESCRIPTION: str = (
    "Set the device's physical face (LED + servo) to match the emotional tone "
    "of the reply you are ABOUT TO SPEAK. This is FIRE-AND-FORGET: "
    "it does NOT delegate and does NOT replace "
    "speech — call it IN PARALLEL with speaking, then immediately speak your reply. "
    "For a request requiring the main agent, a brief spoken acknowledgment and "
    "this emotion call never replace delegate_to_main: call delegate_to_main "
    "immediately in the same turn, whether or not you speak an acknowledgment. "
    "Never wait for it, never mention it, never speak the emotion name or any "
    "marker syntax aloud. Calling it is optional; only call it when an emotion "
    "clearly fits your reply. Available emotions: " + ", ".join(EMOTION_TOOL_EMOTIONS) + "."
)

EMOTION_TOOL: dict[str, Any] = {
    "type": "function",
    "name": EMOTION_TOOL_NAME,
    "description": EMOTION_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "emotion": {
                "type": "string",
                "enum": EMOTION_TOOL_EMOTIONS,
                "description": "The facial emotion that matches the tone of the reply you are about to speak.",
            },
            "intensity": {
                "type": "number",
                "description": "Expression strength from 0.0 (subtle) to 1.0 (full). Defaults to about 0.8.",
            },
        },
        "required": ["emotion"],
    },
}

# A large swing still rings past 300ms; each 30 deg buys 200ms extra, capped.
CAPTURE_SETTLE_BASE_S: float = 0.3
CAPTURE_SETTLE_MAX_S: float = 0.5
CAPTURE_SETTLE_PER_DEG_S: float = 0.0067


def _capture_settle_s(res: Any) -> float:
    """Settle time for the shutter, scaled to how far the head last moved."""
    move = abs(float(getattr(res, "last_move_deg", 0.0) or 0.0))
    return min(CAPTURE_SETTLE_MAX_S, CAPTURE_SETTLE_BASE_S + CAPTURE_SETTLE_PER_DEG_S * move)


LOOK_TOOL_NAME: str = "look"
LOOK_TOOL_DESCRIPTION: str = (
    "Capture a single frame from the device's camera and look at it, so you can "
    "answer a question about what you SEE right now (e.g. 'what is this?', 'what "
    "am I holding?', 'what's in front of you?', 'read this label', 'what color is "
    "this?'). Unlike delegate_to_main, this does NOT hand off — call it, the image "
    "is added to your context, then you immediately SPEAK your answer in this same "
    "turn. The scene CHANGES constantly: the user may have swapped objects since "
    "the last image, so for ANY present-tense visual question you MUST call this "
    "again — never answer from a previous image, from memory, or from the "
    "conversation, even if you are sure you already know; that is exactly how you "
    "get it embarrassingly wrong. Do NOT use it for non-visual requests. Do NOT "
    "use it to find or locate a specific object or person the user is asking "
    "about ('where is my pen', 'do you see my keys', 'look for my pen', 'can you "
    "find my cup') — one frame from wherever the head already points cannot find "
    "anything; that is a search the device performs by moving, so delegate_to_main "
    "it instead of looking and guessing."
)

LOOK_TOOL: dict[str, Any] = {
    "type": "function",
    "name": LOOK_TOOL_NAME,
    "description": LOOK_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {},
    },
}


def _camera_present() -> bool:
    """True when a camera is wired up (the device's `vision` capability at runtime)."""
    try:
        import hal.app_state as state

        return state.camera_capture is not None
    except Exception:
        return False


WEB_SEARCH_TOOL_NAME: str = "web_search"
WEB_SEARCH_TOOL_DESCRIPTION: str = (
    "Look up a PUBLIC, CURRENT fact on the web and get a short grounded answer "
    "back: weather, news, sports scores, prices, exchange rates, opening hours, "
    "release dates, anything that changes over time and is not already in your "
    "context. Unlike delegate_to_main this does NOT hand off — call it with the "
    "question, the answer is added to your context, then you immediately SPEAK "
    "it in this same turn, in the user's language. Do NOT use it for general "
    "knowledge you already hold, for casual conversation, for the user's own "
    "private data (their calendar, messages, devices, memories — delegate "
    "those), or for any action. Call it at most once per question. If the "
    "result is an error, say briefly that you could not check right now — "
    "never guess a live fact."
)

WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "function",
    "name": WEB_SEARCH_TOOL_NAME,
    "description": WEB_SEARCH_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The question to answer, self-contained (resolve 'there', "
                    "'it', 'tomorrow' from the conversation into explicit "
                    "places, names and dates), in the user's spoken language. "
                    "Must not be empty."
                ),
            },
        },
        "required": ["query"],
    },
}


def _web_search_available() -> bool:
    """True when the client-side `web_search` tool should be registered (pipecat provider + flag)."""
    return (
        config.REALTIME_PIPECAT_WEB_SEARCH
        and config.REALTIME_PROVIDER.strip().lower() == "pipecat_v1"
    )


def _make_memory_summarizer() -> RealtimeSummarizer:
    """Summarizer for realtime memory, with thinking off.

    With provider defaults the proxy spent the whole 4096-token budget on
    reasoning and returned no text (stop_reason=max_tokens, device-observed
    2026-09-28), leaving memory.jsonl unsummarized (#449).
    """
    return RealtimeSummarizer(disable_thinking=True)


class RealtimeOrchestrator:
    """Manages a single realtime voice agent session (registers delegate_to_main automatically)."""

    CONTEXT_MANAGERS = CONTEXT_MANAGERS

    WORKSPACE_DIRS: dict[str, str] = {
        AgentGateway.OPENCLAW: config.OPENCLAW_WORKSPACE_DIR,
        AgentGateway.HERMES: config.HERMES_WORKSPACE_DIR,
        AgentGateway.PICOCLAW: config.PICOCLAW_WORKSPACE_DIR,
        AgentGateway.CODEX: config.CODEX_WORKSPACE_DIR,
        AgentGateway.CLAUDECODE: config.CLAUDECODE_WORKSPACE_DIR,
        AgentGateway.OPENCODE: config.OPENCODE_WORKSPACE_DIR,
    }

    def __init__(
        self,
        gateway: AgentGateway = AgentGateway.OPENCLAW,
        extra_tools: list[dict[str, Any]] | None = None,
        enable_expression: bool = False,
        stt_provider: Any = None,
        voice_override: Callable[[], str | None] | None = None,
    ) -> None:
        # Gemini Live voice override (the Gemini TTS voice), or None for the config voice.
        self._voice_override: Callable[[], str | None] | None = voice_override
        # Shared STT provider for providers that run STT themselves (pipecat_v1).
        self._stt_provider: Any = stt_provider
        # Registered only when the device declares the `expression` capability.
        self._expression_enabled: bool = enable_expression
        tools: list[dict[str, Any]] = [DELEGATE_TOOL]
        if config.REALTIME_AI_REJECT_FILTER:
            tools.append(REJECT_TURN_TOOL)
        # Live mode only: no open session to hang up on the turn-based path.
        if config.LIVE_MODE:
            tools.append(END_CALL_TOOL)
        if enable_expression:
            tools.append(EMOTION_TOOL)
        # `look` requires a camera, REALTIME_GEMINI_VISION and the Gemini provider; else delegate.
        self._vision_enabled: bool = (
            _camera_present()
            and config.REALTIME_GEMINI_VISION
            and config.REALTIME_PROVIDER.strip().lower() == "gemini"
        )
        if self._vision_enabled:
            tools.append(LOOK_TOOL)
        self._web_search_enabled: bool = _web_search_available()
        if self._web_search_enabled:
            tools.append(WEB_SEARCH_TOOL)
        self._tools: list[dict[str, Any]] = tools + (extra_tools or [])
        # `look` cost guards: one image per turn, none within VISION_MIN_INTERVAL_S of the last.
        self._looked_this_turn: bool = False
        self._last_look_sent_monotonic: float = 0.0
        self._agent: VoiceAgentBase | None = None
        self._started: threading.Event = threading.Event()
        # A failed first connect() has no provider loops to retry it; the orchestrator owns the retry.
        self._connect_retry_stop: threading.Event = threading.Event()
        self._connect_retry_thread: threading.Thread | None = None
        # Dropped on recovery; logged at ERROR only if the first retry also fails.
        self._initial_connect_exc: BaseException | None = None
        # `available` stays True while parked: prepare_turn() reconnects on demand.
        self._idle_parked: bool = False
        self._idle_park_stop: threading.Event = threading.Event()
        self._idle_park_thread: threading.Thread | None = None
        self._park_resume_failed: bool = False
        self._turn_in_flight: bool = False
        self._turn_started_monotonic: float = 0.0
        # Last moment this session saw turn activity (prepare/audio/text/reply
        # end). Seeded at connect so a session idle since boot parks too.
        self._last_activity_monotonic: float = 0.0
        # A reconnect completing after stop must never publish a live agent into the stopped HAL.
        self._lifecycle_lock: threading.Lock = threading.Lock()
        self._consecutive_silent: int = 0  # zombie-session guard (see stream_output)
        # A live session owns the mic; suppresses every post-turn recycle.
        self._live_active: bool = False
        # Idle recycle (cost): rebuild after a turn that followed a long silence.
        self._last_turn_monotonic: float = 0.0  # end-of-turn timestamp; 0 = none yet
        # Idle is measured from the later of this and the last turn.
        self._session_connected_monotonic: float = 0.0
        self._idle_reset_pending: bool = False
        # Gemini already rebuilt before this turn; skip the post-turn idle recycle.
        self._skip_post_idle_recycle: bool = False
        self._turns_since_recycle: int = 0  # turn-cap recycle counter (cost)
        self._rebuild_lock: threading.Lock = threading.Lock()  # guards _force_rebuild
        # Set when no rebuild is in flight; voice capture buffers a turn meanwhile.
        self._rebuild_done: threading.Event = threading.Event()
        self._rebuild_done.set()
        # A user capture preempts an announcement; its commit waits on _announce_idle.
        self._announce_stop: threading.Event | None = None
        self._announce_idle: threading.Event = threading.Event()
        self._announce_idle.set()
        summarizer: RealtimeSummarizer | None = None
        if config.REALTIME_SUMMARIZER_ENABLED:
            try:
                summarizer = _make_memory_summarizer()
                logger.info(
                    "Realtime summarizer enabled (model=%s)",
                    config.REALTIME_SUMMARIZER_MODEL,
                )
            except Exception as e:
                logger.warning("Failed to create summarizer: %s", e)
        context_cls = self.CONTEXT_MANAGERS.get(gateway, OpenClawContextManager)
        self._context: ContextManagerBase = context_cls(
            workspace_dir=self.WORKSPACE_DIRS.get(
                gateway, config.OPENCLAW_WORKSPACE_DIR
            ),
            language=_load_language() or "English",
            provider=config.REALTIME_PROVIDER,
            summarizer=summarizer,
        )

    @property
    def available(self) -> bool:
        # Require a live connection (not just a constructed agent) and no rebuild in flight,
        # so dead or swapping sessions route turns straight to the main agent.
        # A parked session is reported available: prepare_turn() reconnects before audio streams.
        return (
            self._started.is_set()
            and self._agent is not None
            and (
                self._agent.available
                or (self._idle_parked and not self._park_resume_failed)
            )
            and not self._rebuild_lock.locked()
        )

    @property
    def rebuilding(self) -> bool:
        """Whether a replacement provider session is currently connecting."""
        return self._rebuild_lock.locked()

    def wait_until_available(self, timeout_s: float = 2.0) -> bool:
        """Wait briefly for an already-started rebuild without starting one."""
        if self.available:
            return True
        if not self.rebuilding:
            return False
        self._rebuild_done.wait(timeout=max(0.0, timeout_s))
        return self.available

    @property
    def sample_rate(self) -> int:
        """Target sample rate expected by the realtime provider."""
        if self._agent is not None:
            return self._agent.sample_rate
        return DEFAULT_SAMPLE_RATE

    @property
    def output_sample_rate(self) -> int:
        """Sample rate of the model's own audio output (for native playback)."""
        if self._agent is not None:
            return self._agent.output_sample_rate
        return DEFAULT_SAMPLE_RATE

    def _make_agent(self, provider: str, instructions: str) -> VoiceAgentBase | None:
        """Build (not connect) a fresh agent for the provider."""
        if provider == "gemini":
            from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent

            markers = self._use_emotion_markers()
            agent = GeminiLiveAgent(
                config=GeminiConfig(
                    instructions=marker_instructions(instructions) if markers else instructions,
                    voice=GeminiVoice(self._gemini_voice()),
                ),
                tools=[tool for tool in self._tools
                       if not markers or tool.get("name") != EMOTION_TOOL_NAME],
            )
            agent._emotion_markers_enabled = markers
            return agent
        if provider == "openai":
            from hal.realtime.voice_agent.openai_realtime import (
                OpenAIRealtimeAgent,
            )

            return OpenAIRealtimeAgent(
                config=OpenAIConfig(instructions=instructions), tools=self._tools,
            )
        if provider == "gptlive":
            from hal.realtime.voice_agent.gpt_live import GPTLiveAgent

            return GPTLiveAgent(
                config=GPTLiveConfig(instructions=instructions), tools=self._tools,
            )
        if provider == "pipecat_v1":
            from hal.realtime.voice_agent.pipecat_v1 import PipecatV1Agent

            return PipecatV1Agent(
                config=PipecatV1Config(instructions=instructions),
                tools=self._tools,
                stt_provider=self._stt_provider,
            )
        return None

    def _use_emotion_markers(self) -> bool:
        """Only the tested Gemini 3.8 external-TTS path can strip control text."""
        override = getattr(self, "_voice_override", None)
        return bool(
            getattr(self, "_expression_enabled", False)
            and config.REALTIME_GEMINI_MODEL.startswith("gemini-3.8-live")
            and not config.REALTIME_NATIVE_AUDIO
            and not (override and override())
        )

    def _fire_marker_emotion(self, emotion: str, intensity: float) -> None:
        logger.info("[realtime] Emotion marker → express (emotion=%s intensity=%.2f)",
                    emotion, intensity)
        threading.Thread(target=self._fire_emotion, args=(emotion, intensity), daemon=True).start()

    def _gemini_voice(self) -> str:
        """Voice for the next Gemini Live session: the override, else config."""
        voice_override = getattr(self, "_voice_override", None)
        override = voice_override() if voice_override else None
        return override or config.REALTIME_GEMINI_VOICE

    def _begin_rebuild(self) -> bool:
        """Reserve the rebuild slot before a synchronous or background rebuild."""
        if not self._rebuild_lock.acquire(blocking=False):
            return False
        self._rebuild_done.clear()
        return True

    def _finish_rebuild(self) -> None:
        """Publish that the current rebuild has completed, successfully or not."""
        self._rebuild_lock.release()
        self._rebuild_done.set()

    def _rebuild_locked(
        self,
        reason: str,
        discard_old_on_failure: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """Build a replacement session while holding the rebuild reservation."""
        provider: str = config.REALTIME_PROVIDER.strip().lower()
        old = self._agent
        new: VoiceAgentBase | None = None
        try:
            instructions = self._context.build_instructions()
            new = self._make_agent(provider, instructions)
            if new is None:
                if discard_old_on_failure:
                    self._agent = None
                return False
            new.connect()
            with self._lifecycle_lock:
                if (
                    not self._started.is_set()
                    or (cancel_event is not None and cancel_event.is_set())
                ):
                    logger.info(
                        "[realtime] Discarding replacement session — orchestrator stopped"
                    )
                    new.disconnect()
                    return False
                self._agent = new
            self._idle_parked = False
            self._park_resume_failed = False
            self._last_activity_monotonic = time.monotonic()
            self._session_connected_monotonic = self._last_activity_monotonic
            self._consecutive_silent = 0
            self._idle_reset_pending = False
            self._turns_since_recycle = 0
            # A fresh session has no images, so reset the look reuse-guard.
            self._looked_this_turn = False
            self._last_look_sent_monotonic = 0.0
            logger.info("[realtime] Fresh session connected before turn (%s)", reason)
            return True
        except Exception:
            logger.exception("[realtime] Pre-turn session rebuild failed")
            # Tear down a failed connect's transport before retrying, or outages leak workers.
            if new is not None:
                try:
                    new.disconnect()
                except Exception:
                    logger.exception("[realtime] failed replacement disconnect failed")
            # A dropped Manual-VAD activity must never become the next turn; mark it dead.
            if discard_old_on_failure:
                self._agent = None
            return False
        finally:
            self._finish_rebuild()
            if old is not None and old is not self._agent:
                self._disconnect_in_background(old, reason)

    @staticmethod
    def _disconnect_in_background(agent, reason: str) -> None:
        """Tear the replaced session down in the background (inline close cost ~0.8s of turn latency)."""
        def _close() -> None:
            try:
                agent.disconnect()
            except Exception:
                logger.exception("[realtime] old agent disconnect failed (%s)", reason)

        try:
            threading.Thread(
                target=_close, daemon=True, name="rt-old-session-close"
            ).start()
        except Exception:
            # Leaking the socket is worse than the delay.
            logger.warning("[realtime] could not background the old-session close; closing inline")
            _close()

    def _rebuild_now(
        self,
        reason: str,
        cancel_event: threading.Event | None = None,
        discard_old_on_failure: bool = False,
    ) -> bool:
        """Synchronously swap in a fresh session before audio is streamed (Gemini idle-gap recovery)."""
        if not self._begin_rebuild():
            return False
        return self._rebuild_locked(
            reason,
            discard_old_on_failure=discard_old_on_failure,
            cancel_event=cancel_event,
        )

    def _rebuild_in_background(
        self, reason: str, thread_name: str, discard_old_on_failure: bool = False
    ) -> bool:
        """Start a replacement session without blocking voice capture."""
        if not self._begin_rebuild():
            return False
        try:
            threading.Thread(
                target=self._rebuild_locked,
                args=(reason, discard_old_on_failure, None),
                daemon=True,
                name=thread_name,
            ).start()
        except Exception:
            self._finish_rebuild()
            raise
        return True

    def discard_open_activity(self, reason: str) -> bool:
        """Drop a turn whose audio already streamed into the session by starting a fresh session.

        Manual-VAD Gemini has no discard primitive; no-op when no activity is open.
        """
        if self._agent is None or not getattr(self._agent, "_activity_started", False):
            return False
        return self._rebuild_in_background(
            reason, "rt-noise-rebuild", discard_old_on_failure=True
        )

    def recover_session(self, reason: str) -> bool:
        """Reconnect a fresh session synchronously for a mid-turn 1011 replay; True if ready."""
        return self._rebuild_now(reason)

    def set_live_active(self, active: bool) -> None:
        """Mark that a live (full-duplex) session is streaming; suppresses post-turn recycles."""
        self._live_active = active
        if not active:
            # Start the next session from a clean count.
            self._consecutive_silent = 0
            self._turns_since_recycle = 0
            self._idle_reset_pending = False

    def prewarm(self) -> bool:
        """Resume a parked session in the background as soon as speech starts (no-op unless parked)."""
        if not getattr(self, "_idle_parked", False) or self.rebuilding:
            return False
        return self._rebuild_in_background("idle-park-prewarm", "rt-prewarm")

    def _idle_since_monotonic(self) -> float:
        """Start of the current session's idle gap: last turn end or connect."""
        return max(
            self._last_turn_monotonic,
            getattr(self, "_session_connected_monotonic", 0.0),
        )

    def prepare_turn(self) -> None:
        """Prepare the realtime session before the caller streams turn audio."""
        # A new capture starts a new logical turn.
        self._skip_post_idle_recycle = False
        self._last_activity_monotonic = time.monotonic()
        self._turn_started_monotonic = time.monotonic()
        # Mark the turn BEFORE checking announcements: announce() does the reverse, so one always sees the other.
        self._turn_in_flight = True
        # The user always wins over a device announcement.
        stop = getattr(self, "_announce_stop", None)
        if stop is not None and not stop.is_set():
            logger.info("[realtime] User capture preempts the running announcement")
            stop.set()
        self._prepare_session()

    def _prepare_session(self) -> None:
        """Resume/replace the provider session so the next input reaches a live one."""
        provider: str = config.REALTIME_PROVIDER.strip().lower()
        agent = getattr(self, "_agent", None)
        if getattr(self, "_idle_parked", False):
            # Join a prewarm() resume in flight instead of falling back.
            if self.rebuilding:
                self._rebuild_done.wait(timeout=PREWARM_JOIN_TIMEOUT_S)
                if not self._idle_parked:
                    self._skip_post_idle_recycle = True
                    return
            # Parked: connect a fresh session before any audio is streamed.
            if self._rebuild_now("idle-park-resume"):
                self._skip_post_idle_recycle = True
                return
            # Stay parked for the next retry, but report unavailable: the transport is closed.
            self._park_resume_failed = True
            logger.warning(
                "[realtime] Could not resume parked session — falling back for this turn"
            )
            return
        # An unanswered Gemini tool call (or a handoff) makes the session non-reusable (1008).
        if (
            provider == "gemini"
            and agent is not None
            and agent.requires_fresh_session
        ):
            logger.info(
                "[realtime] Rebuilding Gemini session after unresolved tool or handoff before streaming audio"
            )
            if self._rebuild_now(
                "gemini-unresolved-tool-call", discard_old_on_failure=True
            ):
                self._skip_post_idle_recycle = True
            return
        # The voice is fixed at connect; a voice change needs a fresh session.
        if provider == "gemini" and agent is not None:
            marker_mode = getattr(agent, "_emotion_markers_enabled", None)
            if isinstance(marker_mode, bool) and marker_mode != self._use_emotion_markers():
                if self._rebuild_now("gemini-expression-mode-change", discard_old_on_failure=True):
                    self._skip_post_idle_recycle = True
                return
            current = getattr(getattr(agent, "_config", None), "voice", None)
            wanted = self._gemini_voice()
            if current is not None and current != wanted:
                logger.info("[realtime] Gemini voice %s -> %s — rebuilding session", current, wanted)
                if self._rebuild_now("gemini-voice-change", discard_old_on_failure=True):
                    self._skip_post_idle_recycle = True
                return
        # Not gated on gemini_needs_idle_workaround(): Gemini kills idle sessions on every model (1008).
        if provider != "gemini":
            return
        threshold = config.REALTIME_GEMINI_PRE_TURN_RECYCLE_S
        if threshold <= 0 or self._last_turn_monotonic <= 0.0:
            return
        # Session idle, not user idle: after an unmute the session is only seconds old.
        idle = time.monotonic() - self._idle_since_monotonic()
        if idle < threshold:
            return
        logger.info(
            "[realtime] %.0fs idle (>= %.0fs) — recycling Gemini before streaming audio",
            idle,
            threshold,
        )
        if self._rebuild_now("gemini-idle-pre-turn"):
            self._skip_post_idle_recycle = True

    def _start_idle_park_loop(self) -> None:
        """Run the idle watchdog that closes a session nobody is using."""
        if self._idle_park_thread is not None and self._idle_park_thread.is_alive():
            return
        self._idle_park_stop.clear()
        self._idle_park_thread = threading.Thread(
            target=self._idle_park_loop,
            daemon=True,
            name="rt-idle-park",
        )
        self._idle_park_thread.start()

    def _idle_park_loop(self) -> None:
        while not self._idle_park_stop.wait(IDLE_PARK_POLL_S):
            try:
                self._maybe_park_idle_session()
            except Exception:
                logger.exception("[realtime] Idle park check failed")

    def _maybe_park_idle_session(self) -> None:
        """Close the transport before the server's own idle kill (WS 1008) pages the backend."""
        threshold: float = self._idle_park_threshold()
        if threshold <= 0:
            return
        if not self._started.is_set() or self._idle_parked:
            return
        agent = self._agent
        if agent is None or not agent.available:
            return  # already down — the reconnect paths own it
        if self._rebuild_lock.locked():
            return  # a replacement session is already on its way
        now: float = time.monotonic()
        if (
            self._turn_in_flight
            and now - self._turn_started_monotonic < TURN_IN_FLIGHT_MAX_S
        ):
            return  # a turn is mid-flight; parking would kill it
        last: float = max(self._last_activity_monotonic, self._last_turn_monotonic)
        if last <= 0.0 or now - last < threshold:
            return
        self._park_idle_session(now - last)

    @staticmethod
    def _idle_park_threshold() -> float:
        """Seconds of inactivity before the provider session is parked; 0 = never.

        Gemini: avoid the server idle kill. GPT-Live: billed per minute. OpenAI and Pipecat: never.
        """
        provider: str = config.REALTIME_PROVIDER.strip().lower()
        if provider == "gemini":
            return config.REALTIME_GEMINI_IDLE_PARK_S
        if provider == "gptlive":
            return config.REALTIME_GPTLIVE_IDLE_PARK_S
        return 0.0

    def _park_idle_session(self, idle_s: float) -> None:
        """Disconnect the current session and mark it resumable (under the rebuild reservation)."""
        if not self._begin_rebuild():
            return
        try:
            agent = self._agent
            if agent is None:
                return
            logger.info(
                "[realtime] %.0fs idle (>= %.0fs) — parking %s session "
                "(closing before the server does / before it bills more)",
                idle_s,
                self._idle_park_threshold(),
                config.REALTIME_PROVIDER.strip().lower(),
            )
            try:
                agent.disconnect()
            except Exception:
                logger.exception("[realtime] Idle park disconnect failed")
                return
            self._idle_parked = True
            self._park_resume_failed = False
        finally:
            self._finish_rebuild()

    def _force_rebuild(self) -> None:
        """Recover a zombie session by building a brand-new agent and swapping it in.

        The old recv thread is wedged on a dead socket, so an in-place reconnect never fires.
        """
        self._rebuild_in_background("zombie-recovery", "rt-zombie-rebuild")

    def _start_connect_retry_loop(self) -> None:
        """Reconnect after the initial provider connection failed (off the voice thread, bounded backoff)."""
        if (
            self._connect_retry_thread is not None
            and self._connect_retry_thread.is_alive()
        ):
            return
        self._connect_retry_thread = threading.Thread(
            target=self._connect_retry_loop,
            daemon=True,
            name="rt-initial-connect-retry",
        )
        self._connect_retry_thread.start()

    def _connect_retry_loop(self) -> None:
        """Try fresh sessions until the initial connection recovers or HAL stops."""
        delay_s = INITIAL_CONNECT_RETRY_DELAY_S
        while not self._connect_retry_stop.is_set():
            if self._rebuild_now(
                "initial-connect-retry", cancel_event=self._connect_retry_stop
            ):
                # Recovered: the transient failure's traceback is dropped.
                self._initial_connect_exc = None
                logger.info("[realtime] Initial connection recovered automatically")
                return
            if self._initial_connect_exc is not None:
                logger.error(
                    "[realtime] Failed to connect realtime agent — retry did not "
                    "recover it",
                    exc_info=self._initial_connect_exc,
                )
                self._initial_connect_exc = None
            logger.warning(
                "[realtime] Initial connection still unavailable — retrying in %.0fs",
                delay_s,
            )
            if self._connect_retry_stop.wait(delay_s):
                return
            delay_s = min(delay_s * 2, INITIAL_CONNECT_RETRY_MAX_DELAY_S)

    def start(self) -> None:
        """Create the agent based on config and connect."""
        provider: str = config.REALTIME_PROVIDER.strip().lower()
        if provider in ("none", "off", "disabled", ""):
            logger.info("Realtime orchestrator disabled (provider=%s)", provider)
            return

        self._connect_retry_stop.clear()
        self._initial_connect_exc = None

        instructions: str = self._context.build_instructions()
        logger.info(
            "[realtime] Context manager built instructions (%d chars)",
            len(instructions),
        )

        self._agent = self._make_agent(provider, instructions)
        if self._agent is None:
            logger.warning("Unknown realtime provider: %s — disabled", provider)
            return

        try:
            self._agent.connect()
            self._session_connected_monotonic = time.monotonic()
            logger.info(
                "[realtime] Realtime orchestrator started (provider=%s)", provider
            )
        except Exception as e:
            # One WARNING now; _connect_retry_loop logs ERROR only if the retry does not recover.
            self._initial_connect_exc = e
            logger.warning(
                "[realtime] Failed to connect realtime agent (%s: %s) — "
                "retrying in background",
                type(e).__name__, e,
            )
        self._started.set()
        self._last_activity_monotonic = time.monotonic()
        if not self.available:
            self._start_connect_retry_loop()
        self._start_idle_park_loop()

        # Background: running before connect would keep `available` False and leak early turns.
        threading.Thread(
            target=self._catch_up_memory_summaries,
            daemon=True,
            name="realtime-catchup-summarize",
        ).start()

    def _catch_up_memory_summaries(self) -> None:
        """Summarize memory left unsummarized by a previous session (background)."""
        try:
            self._context.summarize_device_memory()
            self._context.summarize_realtime_memory()
        except Exception:
            logger.exception("[realtime] Failed to catch up on memory summarization")

    def stop(self) -> None:
        """Disconnect the agent and summarize unsummarized memory."""
        self._connect_retry_stop.set()
        self._idle_park_stop.set()
        self._idle_parked = False
        with self._lifecycle_lock:
            self._started.clear()
            agent = self._agent
            self._agent = None
        try:
            self._context.summarize_device_memory()
            self._context.summarize_realtime_memory()
        except Exception:
            logger.exception("[realtime] Failed to summarize memory on shutdown")

        if agent is not None:
            try:
                agent.disconnect()
            except Exception:
                logger.exception("Failed to disconnect realtime agent")
        logger.info("Realtime orchestrator stopped")

    def bind_audio_turn(self) -> AudioTurnBinding:
        """Bind after prepare_turn; a lost binding requires full audio replay."""
        with self._lifecycle_lock:
            agent = self._agent
            if not self.available or agent is None:
                raise AudioTurnSessionChanged("No ready session for audio turn")
            session = agent.audio_session
            if session is None:
                raise AudioTurnSessionChanged("Provider transport is not connected")
            agent.validate_audio_session(session)
            return AudioTurnBinding(agent=agent, session=session)

    def _validate_audio_turn(self, turn: AudioTurnBinding) -> VoiceAgentBase:
        if self._agent is not turn.agent or not self._started.is_set():
            raise AudioTurnSessionChanged("Audio turn orchestrator session changed")
        turn.agent.validate_audio_session(turn.session)
        return turn.agent

    def append_audio(self, frame: npt.NDArray[np.float32], *, turn: AudioTurnBinding | None = None) -> None:
        """Queue a single audio frame to the model (non-blocking)."""
        self._last_activity_monotonic = time.monotonic()
        if turn is not None:
            self._validate_audio_turn(turn).append_audio(frame, session=turn.session)
            return
        if self._agent is not None:
            self._agent.append_audio(frame)

    def end_live_audio(self) -> bool:
        """Queue the paused live uplink boundary on the current transport only."""
        with self._lifecycle_lock:
            agent = self._agent
            if agent is None or not agent.available:
                return False
            session = agent.audio_session
            if session is None:
                return False
            return agent.end_audio_stream(session=session)

    def commit_audio(self, *, turn: AudioTurnBinding | None = None) -> None:
        """Queue commit signal (non-blocking)."""
        if turn is not None:
            agent = self._validate_audio_turn(turn)
            self._mark_turn_start()
            agent.commit_audio(session=turn.session)
            return
        self._mark_turn_start()
        if self._agent is not None:
            self._agent.commit_audio()

    def _mark_turn_start(self) -> None:
        """Arm a post-turn session recycle after a long idle gap (cost: drop re-billed context).

        Only arms here; the rebuild fires at the end of stream_output, never mid-commit.
        """
        if self._skip_post_idle_recycle:
            # prepare_turn() already connected a fresh session for this idle gap.
            return

        reset_s = config.REALTIME_SESSION_IDLE_RESET_S
        if reset_s <= 0 or self._last_turn_monotonic <= 0.0:
            return
        # A session connected after the last turn holds no context to drop.
        idle = time.monotonic() - self._idle_since_monotonic()
        if idle >= reset_s:
            logger.info(
                "[realtime] %.0fs idle (>= %.0fs) — will recycle session after this "
                "turn to drop accumulated context (cost)",
                idle,
                reset_s,
            )
            self._idle_reset_pending = True

    def flush_output(self, *, turn: AudioTurnBinding | None = None) -> None:
        """Discard buffered outputs from a prior turn before committing a new one."""
        idle = getattr(self, "_announce_idle", None)
        if idle is not None and not idle.wait(timeout=ANNOUNCE_DRAIN_S):
            logger.warning("[realtime] Announcement still draining — committing anyway")
        if turn is not None:
            self._validate_audio_turn(turn).flush_output()
            return
        if self._agent is not None:
            self._agent.flush_output()

    def stream_output(
        self,
        *, turn: AudioTurnBinding | None = None,
        stop_event: threading.Event | None = None,
    ) -> Generator[OutputBase | DelegateSignal | RejectSignal | LookReplaySignal, None, None]:
        """Yield model outputs as they arrive until the turn is done.

        Stops after DelegateSignal, RejectSignal or LookReplaySignal (caller re-commits the audio).
        """
        self.execution_completed = False
        self.execution_turn_id = ""
        self.intentional_silence = False
        if turn is not None:
            self._validate_audio_turn(turn)
        if self._agent is None:
            return
        execution_agent = self._agent
        marker_stream = (EmotionMarkerStream(EMOTION_TOOL_EMOTIONS, self._fire_marker_emotion)
                         if getattr(execution_agent, "_emotion_markers_enabled", False) is True
                         else None)
        marker_turn_id = ""

        self._looked_this_turn = False  # reset the per-turn `look` image-send guard
        produced = False  # did this turn yield any real output (vs stay silent)?
        rejected = False
        replay_pending = False  # look-replay signalled — the turn continues
        silence_text: list[str] = []
        had_tool_or_interruption = False
        receive_kwargs: dict[str, Any] = {"stop_on_done": True}
        if stop_event is not None:
            receive_kwargs["stop_event"] = stop_event
        for output in execution_agent.receive(**receive_kwargs):
            if stop_event is not None and stop_event.is_set():
                return
            # A session that is still streaming a reply is not idle. Without
            # this, a reply longer than TURN_IN_FLIGHT_MAX_S (a 124-143 s recap,
            # device-observed 2026-09-29) was parked mid-sentence: the in-flight
            # guard had expired and the last activity was the commit.
            self._last_activity_monotonic = time.monotonic()
            if turn is not None:
                self._validate_audio_turn(turn)
            if marker_stream is not None:
                if isinstance(output, InterruptedOutput):
                    marker_stream.reset()
                if output.user_turn_id and output.user_turn_id != marker_turn_id:
                    marker_stream.reset()
                    marker_turn_id = output.user_turn_id
                if isinstance(output, TextOutput):
                    clean = marker_stream.feed(output.text)
                    if not clean:
                        continue
                    output = output.model_copy(update={"text": clean})
            if isinstance(output, (FunctionCallOutput, MainAgentFallbackOutput, InterruptedOutput)):
                had_tool_or_interruption = True
            if isinstance(output, TextOutput):
                silence_text.append(output.text)
            elif isinstance(output, AudioOutput) and output.transcript:
                silence_text.append(output.transcript)
            if isinstance(output, MainAgentFallbackOutput):
                # Local fail-safe, not a provider call: no call ID to acknowledge.
                produced = True
                execution_agent.end_turn()
                yield DelegateSignal(
                    message=output.transcript,
                    transcript=output.transcript,
                    user_turn_id=output.user_turn_id,
                    handoff_context=output.handoff_context,
                )
                break
            if isinstance(output, (ExecutionOutput, TextSegmentEndOutput)):
                yield output
                continue
            if isinstance(output, InterruptedOutput) and output.reason == "server_interrupt":
                # Must not change this generator's tool/routing state.
                yield output
                continue
            if isinstance(output, UserSpeechOutput):
                # Input observations are not an answer.
                yield output
                continue
            if (
                isinstance(output, FunctionCallOutput)
                and output.name == LOOK_TOOL_NAME
            ):
                # A find is a servo search, never a look (see _redirect_find_look).
                redirect = self._redirect_find_look(output)
                if redirect is not None:
                    produced = True
                    yield redirect
                    break
                # Fresh frame sent: the turn must be REPLAYED so the queued frame joins the answer.
                if self._handle_look_call(output):
                    produced = True
                    replay_pending = True
                    # No turn_complete follows a tool-only turn; also swallow the cancelled turn's late one.
                    self._agent.end_turn()
                    self._agent.skip_next_turn_done()
                    yield LookReplaySignal()
                    break
                continue
            if (
                isinstance(output, FunctionCallOutput)
                and output.name == WEB_SEARCH_TOOL_NAME
            ):
                # Blocking lookup (~4 s); the model's follow-up reply is the spoken answer.
                self._handle_web_search_call(output)
                produced = True
                continue
            if (
                isinstance(output, FunctionCallOutput)
                and output.name == EMOTION_TOOL_NAME
            ):
                # `produced` decides whether the ack is safe (see _handle_emotion_call).
                self._handle_emotion_call(output, spoken=produced)
                continue
            if (
                isinstance(output, FunctionCallOutput)
                and output.name == END_CALL_TOOL_NAME
            ):
                logger.info("[realtime] Model asked to end the conversation")
                # Ack with trigger_response=True: the farewell must finish, and an unacked call
                # makes Gemini reject all later input.
                self._agent.send(
                    [
                        FunctionCallResultInput(
                            call_id=output.call_id,
                            output='{"result": "ending after farewell"}',
                        )
                    ]
                )
                produced = True
                yield EndCallSignal()
                continue
            if (
                isinstance(output, FunctionCallOutput)
                and output.name == REJECT_TURN_TOOL_NAME
            ):
                # LIVE output can be queued before the routing tool arrives; honor explicit rejection.
                if produced and not config.LIVE_MODE:
                    logger.warning(
                        "[realtime] Ignoring reject_turn after output already began"
                    )
                    self._agent.send(
                        [
                            FunctionCallResultInput(
                                call_id=output.call_id,
                                output='{"error": "reject_turn must be the only turn outcome"}',
                            )
                        ]
                    )
                    # End rather than letting an ack generate a second response.
                    self._agent.end_turn()
                    break
                rejected = True
                logger.info("[realtime] Model explicitly rejected this turn")
                # Ack like delegation so no pending tool call poisons the next manual-VAD activity.
                self._agent.send(
                    [
                        FunctionCallResultInput(
                            call_id=output.call_id,
                            output='{"result": "turn dropped"}',
                        )
                    ]
                )
                produced = True
                self._agent.end_turn()
                yield RejectSignal(user_turn_id=output.user_turn_id)
                break
            if (
                isinstance(output, FunctionCallOutput)
                and output.name == DELEGATE_TOOL_NAME
            ):
                delegate_msg: str = ""
                try:
                    args: dict[str, Any] = (
                        json.loads(output.arguments) if output.arguments else {}
                    )
                    delegate_msg = args.get("message", "").strip()
                except (ValueError, TypeError):
                    pass

                if not delegate_msg:
                    logger.warning(
                        "[realtime] Model called delegate_to_main with empty message — ignoring"
                    )
                    self._agent.send(
                        [
                            FunctionCallResultInput(
                                call_id=output.call_id,
                                output='{"error": "message must not be empty"}',
                            )
                        ]
                    )
                    continue

                logger.info(
                    "[realtime] Model delegated to main flow (message=%r)",
                    delegate_msg[:100],
                )
                # A quarantined manual Gemini session will be replaced before the next
                # capture anyway. Do not ask it to generate an unused handoff response.
                # Live sessions still need the ACK to keep their continuous input usable.
                abandon_handoff = (
                    config.REALTIME_PROVIDER.strip().lower() == "gemini"
                    and not config.LIVE_MODE
                    and not getattr(self, "_live_active", False)
                    and not config.REALTIME_GEMINI_SESSION_RESUMPTION
                    and getattr(
                        getattr(self._agent, "_config", None),
                        "session_resumption_enabled", True,
                    ) is False
                    and getattr(self._agent, "requires_fresh_session", False) is True
                )
                self._agent.send(
                    [
                        FunctionCallResultInput(
                            call_id=output.call_id,
                            output='{"result": "delegated"}',
                            trigger_response=not abandon_handoff,
                        )
                    ]
                )
                produced = True
                # No turn_complete follows a tool call; don't gate the next commit on it.
                self._agent.end_turn()
                yield DelegateSignal(
                    message=delegate_msg, transcript=output.user_transcript,
                    user_turn_id=output.user_turn_id,
                    handoff_context=output.handoff_context,
                )
                # Forward immediately: waiting for turn_complete after a delegate blocks ~15s.
                break
            produced = True
            yield output

        if turn is not None:
            self._validate_audio_turn(turn)

        # Session cancellation must not trigger lifecycle accounting or a rebuild.
        if stop_event is not None and stop_event.is_set():
            return

        # Capture the consumed terminal before any post-turn recycle swaps agents.
        self.execution_completed = (
            getattr(execution_agent, "execution_completed", False) is True
            and not replay_pending
            and not rejected
        )
        self.execution_turn_id = getattr(execution_agent, "execution_turn_id", "")
        # A complete explicit silence marker is different from an empty socket
        # timeout. Never infer rejection from silence alone, partial markers,
        # marker-prefixed answers, or a turn that performed tools. The manual
        # text-to-TTS consumer decides whether anything was already spoken.
        self.intentional_silence = bool(
            self.execution_completed
            and config.REALTIME_PROVIDER.strip().lower() == "gemini"
            and not had_tool_or_interruption
            and re.fullmatch(r"\s*(?:<\s*no\s+speech\s*>\s*)+", "".join(silence_text), re.IGNORECASE)
        )

        # Look replay continues the same logical turn: recycling now would orphan the sent image.
        if replay_pending:
            return

        skip_post_idle_recycle = self._skip_post_idle_recycle
        self._skip_post_idle_recycle = False

        # Zombie guard: N consecutive silent turns force a fresh session.
        if produced:
            self._consecutive_silent = 0
        else:
            self._consecutive_silent += 1

        # Stamp end-of-turn; also releases the idle watchdog's in-flight hold.
        self._last_turn_monotonic = time.monotonic()
        self._last_activity_monotonic = self._last_turn_monotonic
        self._turn_in_flight = False
        self._turns_since_recycle += 1

        # Post-turn recycle (zombie / idle / turn-cap), decided in one place, async, between turns.
        # No grounding-triggered recycle: it raced the next turn. All recycles are deferred
        # while a live session owns the mic (set_live_active(False) resets the counters).
        if getattr(self, "_live_active", False):
            return

        zombie: bool = (
            not produced
            and self._consecutive_silent >= config.REALTIME_ZOMBIE_RECONNECT_AFTER
        )
        max_turns: int = config.REALTIME_SESSION_MAX_TURNS
        turn_cap: bool = max_turns > 0 and self._turns_since_recycle >= max_turns
        idle_recycle: bool = (
            self._idle_reset_pending and not skip_post_idle_recycle
        )
        if zombie or idle_recycle or turn_cap:
            if zombie:
                logger.warning(
                    "[realtime] %d consecutive silent turns — forcing reconnect "
                    "(zombie session)",
                    self._consecutive_silent,
                )
            else:
                reason: str = "idle" if idle_recycle else "turn-cap"
                logger.info(
                    "[realtime] recycling session (%s) after %d turns (cost)",
                    reason, self._turns_since_recycle,
                )
            self._consecutive_silent = 0
            self._idle_reset_pending = False
            self._turns_since_recycle = 0
            self._force_rebuild()

    @property
    def turn_in_flight(self) -> bool:
        """A user turn is between prepare_turn() and the end of its reply."""
        return bool(
            getattr(self, "_turn_in_flight", False)
            and time.monotonic() - getattr(self, "_turn_started_monotonic", 0.0)
            < TURN_IN_FLIGHT_MAX_S
        )

    def finish_capture(self) -> None:
        """HAL has finished processing a capture; none of its output is still read here."""
        self._turn_in_flight = False

    @property
    def parked(self) -> bool:
        """The idle watchdog closed the transport; the next turn reconnects."""
        return bool(getattr(self, "_idle_parked", False))

    @property
    def supports_announce(self) -> bool:
        """Whether the configured provider can speak an announcement at all."""
        agent = self._agent
        return bool(self._started.is_set() and agent is not None and agent.supports_announce)

    def prepare_announcement(self, *, allow_resume: bool) -> bool:
        """Ready the session for announce(); False means render without realtime.

        allow_resume=False refuses to reconnect a parked session.
        """
        if not self.supports_announce or self.turn_in_flight:
            return False
        if self.parked and not allow_resume:
            return False
        if self.rebuilding:
            self._rebuild_done.wait(timeout=PREWARM_JOIN_TIMEOUT_S)
            if self.turn_in_flight:
                return False
        self._last_activity_monotonic = time.monotonic()
        self._prepare_session()
        agent = self._agent
        if agent is not None and getattr(agent, "_activity_started", False):
            # An abandoned Manual-VAD activity would split on text; replace the session first.
            logger.info("[realtime] Abandoned activity open — fresh session before announcing")
            self._rebuild_now("announce-abandoned-activity", discard_old_on_failure=True)
            agent = self._agent
        return bool(
            self.available and agent is not None and agent.available
            and agent.supports_announce and not agent.requires_fresh_session
        )

    def announce(
        self, text: str, *, stop_event: threading.Event,
    ) -> Generator[OutputBase | DelegateSignal | RejectSignal | LookReplaySignal, None, None]:
        """Speak a device-initiated update; yields like stream_output().

        Call prepare_announcement() first; a user capture stops it and drains the rest.
        """
        agent = self._agent
        if agent is None:
            return
        self._announce_idle.clear()
        self._announce_stop = stop_event
        try:
            if self.turn_in_flight:
                # A capture began after prepare_announcement(); it owns the session.
                logger.info("[realtime] Announcement skipped — a user turn just started")
                stop_event.set()
                return
            agent.flush_output()
            if not agent.announce(text):
                return
            agent.extend_recv_timeout(ANNOUNCE_RECV_TIMEOUT_S)
            now = time.monotonic()
            self._last_activity_monotonic = now
            self._turn_in_flight = True
            self._turn_started_monotonic = now
            logger.info("[realtime] Announcement sent (%d chars)", len(text))
            yield from self.stream_output(stop_event=stop_event)
            if stop_event.is_set():
                self._drain_announcement(agent)
        finally:
            # The user's turn must start from the default watchdog.
            agent._recv_timeout_override_s = None
            self._announce_stop = None
            self._announce_idle.set()

    @staticmethod
    def _drain_announcement(agent: VoiceAgentBase) -> None:
        """Read a preempted announcement to its end so none of it is spoken later."""
        drain_stop = threading.Event()
        timer = threading.Timer(ANNOUNCE_DRAIN_S, drain_stop.set)
        timer.daemon = True
        timer.start()
        dropped = 0
        try:
            for _ in agent.receive(stop_on_done=True, stop_event=drain_stop):
                dropped += 1
        except Exception:
            logger.exception("[realtime] Announcement drain failed")
        finally:
            timer.cancel()
        agent.end_turn()
        logger.info("[realtime] Preempted announcement drained (%d output(s) dropped)", dropped)

    def _handle_web_search_call(self, output: FunctionCallOutput) -> None:
        """Answer the model's `web_search` call with a grounded result (synchronous, bounded).

        Always acked with trigger_response=True; failures point the model at delegate_to_main.
        """
        if self._agent is None:
            return
        query: str = ""
        try:
            args: dict[str, Any] = (
                json.loads(output.arguments) if output.arguments else {}
            )
            query = str(args.get("query", "")).strip()
        except (ValueError, TypeError):
            pass

        if not query:
            logger.warning("[realtime][web_search] called with empty query — ignoring")
            result_json = '{"error": "query must not be empty"}'
        else:
            from hal.realtime.web_search import WebSearchError, grounded_search

            logger.info("[realtime][web_search] searching: %r", query[:200])
            try:
                res = grounded_search(
                    query,
                    url=config.REALTIME_PIPECAT_SEARCH_URL,
                    api_key=config.REALTIME_PIPECAT_SEARCH_API_KEY,
                    model=config.REALTIME_PIPECAT_SEARCH_MODEL,
                    timeout_s=config.REALTIME_PIPECAT_SEARCH_TIMEOUT_S,
                )
            except WebSearchError as e:
                logger.warning("[realtime][web_search] failed: %s", e)
                result_json = json.dumps(
                    {
                        "error": str(e),
                        "hint": "Tell the user you could not check right now, or "
                        "call delegate_to_main with their words.",
                    }
                )
            else:
                logger.info(
                    "[realtime][web_search] answered in %.2fs (queries=%s sources=%s): %r",
                    res.elapsed_s, res.queries, res.sources, res.answer[:120],
                )
                result_json = json.dumps(
                    {"result": res.answer, "sources": res.sources},
                    ensure_ascii=False,
                )

        self._agent.send(
            [FunctionCallResultInput(call_id=output.call_id, output=result_json)]
        )

    def _handle_emotion_call(self, output: FunctionCallOutput, *, spoken: bool) -> None:
        """Fire the device's emotion expression without blocking the spoken turn."""
        emotion: str = ""
        intensity: float = DEFAULT_EMOTION_INTENSITY
        try:
            args: dict[str, Any] = (
                json.loads(output.arguments) if output.arguments else {}
            )
            emotion = str(args.get("emotion", "")).strip().lower()
            intensity = float(args.get("intensity", DEFAULT_EMOTION_INTENSITY))
        except (ValueError, TypeError):
            pass
        intensity = max(0.0, min(1.0, intensity))

        if emotion:
            threading.Thread(
                target=self._fire_emotion,
                args=(emotion, intensity),
                daemon=True,
            ).start()
            logger.info(
                "[realtime] express_emotion fired (emotion=%s intensity=%.2f)",
                emotion,
                intensity,
            )
        else:
            logger.warning(
                "[realtime] express_emotion called with empty emotion — ignoring"
            )

        # spoken=True: don't ack, or Gemini re-speaks the whole reply.
        # spoken=False: ack, or Gemini deadlocks waiting for a tool response.
        if self._agent is not None:
            self._agent.send(
                [
                    FunctionCallResultInput(
                        call_id=output.call_id,
                        output='{"result": "expressed", "note": "fire-and-forget; do not react to this ack"}',
                        trigger_response=True, # patch: not to block the session
                    )
                ]
            )

    @staticmethod
    def _fire_emotion(emotion: str, intensity: float) -> None:
        """Drive the device face by calling the HAL emotion handler in-process (daemon thread)."""
        started: float = time.monotonic()
        try:
            # Lazy import keeps app_state/LED/servo out of the module-load graph.
            from hal.models import EmotionRequest
            from hal.routes.emotion import express_emotion as hal_express_emotion

            result: Any = hal_express_emotion(
                EmotionRequest(emotion=emotion, intensity=intensity)
            )
            took_ms: float = (time.monotonic() - started) * 1000
            status: str = (
                result.get("status", "?") if isinstance(result, dict) else "?"
            )
            logger.info(
                "[realtime] emotion expressed (emotion=%s intensity=%.2f status=%s %.0fms)",
                emotion,
                intensity,
                status,
                took_ms,
            )
        except Exception as e:
            logger.warning(
                "[realtime] emotion expression failed (emotion=%s): %s", emotion, e
            )

    def _redirect_find_look(self, output: FunctionCallOutput) -> DelegateSignal | None:
        """Hand a find/search request that reached `look` to the main agent (#481).

        Returns None (normal look) when the transcript is empty or not a find.
        """
        transcript: str = (output.user_transcript or "").strip()
        if not transcript:
            logger.info("[realtime] look: no transcript at call time — find guard skipped")
            return None
        if not is_find_request(transcript):
            return None
        logger.info(
            "[realtime] look: find request %r — delegating without capture",
            transcript[:100],
        )
        import hal.app_state as state

        # Drop any earlier frame so the handoff cannot carry a stale [vision-image].
        state.realtime_look_frame_path = None
        state.realtime_look_frame_ts = 0.0
        self._agent.send(
            [
                FunctionCallResultInput(
                    call_id=output.call_id,
                    output='{"result": "delegated"}',
                )
            ]
        )
        self._agent.end_turn()
        return DelegateSignal(
            message=transcript, transcript=transcript,
            user_turn_id=output.user_turn_id,
        )

    def _handle_look_call(self, output: FunctionCallOutput) -> bool:
        """Handle the model's `look` call.

        Returns True when a fresh frame was sent and the turn must be replayed, False to continue
        (frame reused, no camera, or an Extended Thinking session that continues its interaction).
        """
        if self._agent is None:
            return False
        from hal.drivers.tracking import look_debug

        look_debug.start()
        now: float = time.monotonic()
        # Cost guard (also the replay-turn path): no new image within the interval or twice per turn.
        min_interval: float = config.REALTIME_GEMINI_VISION_MIN_INTERVAL_S
        since_last: float = now - self._last_look_sent_monotonic
        if self._looked_this_turn or (
            min_interval > 0
            and self._last_look_sent_monotonic > 0.0
            and since_last < min_interval
        ):
            reason: str = (
                "already looked this turn"
                if self._looked_this_turn
                else f"{since_last:.1f}s < {min_interval:.0f}s since last send"
            )
            logger.info(
                "[realtime] look: reusing recent frame (%s) — no new image sent (cost)",
                reason,
            )
            # Not a failure: the replayed turn reading the frame captured moments ago.
            look_debug.note_event(f"reused recent frame ({reason})")
            # Reading a frame can keep Gemini thinking silently past the default watchdog.
            self._agent.extend_recv_timeout(config.REALTIME_LOOK_RECV_TIMEOUT_S)
            self._agent.send(
                [
                    FunctionCallResultInput(
                        call_id=output.call_id,
                        output='{"result": "using the current view; answer now"}',
                    )
                ]
            )
            return False

        # Aim before capturing, bounded and never fatal; own the body for aim AND capture
        # so an emotion animation can't re-pose the head in between.
        from hal.drivers.tracking.aim import servo_ownership

        with servo_ownership():
            res: Any = None
            if config.LOOK_AIM_ENABLED:
                try:
                    from hal.drivers.tracking.aim import aim_for_look, filler_ownership
                    from hal.telemetry import voice_metrics

                    # LIVE uses the tool's exact input key; never charge a filler to the newest interaction.
                    try:
                        filler_owner = (
                            voice_metrics.provider_interaction(output.user_turn_id)
                            if config.LIVE_MODE else voice_metrics.current_interaction()
                        )
                    except Exception:
                        logger.exception("[voice-metrics] look filler ownership unavailable")
                        filler_owner = ""

                    t_aim = time.monotonic()
                    with look_debug.stage("aim.total"), filler_ownership(filler_owner):
                        res = aim_for_look(config.LOOK_AIM_DEADLINE_S)
                    logger.info(
                        "[realtime] look: aim %s (%s) iters=%d yaw=%+.1f in %.0fms",
                        "OK" if res.aimed else "skipped",
                        res.reason, res.iterations, res.yaw_moved_deg,
                        (time.monotonic() - t_aim) * 1000,
                    )
                    look_debug.note_aim(res)
                    # Announce the capture only when the aim did work and succeeded.
                    if config.LOOK_AIM_SPEAK_CAPTURE and res.aimed and res.iterations > 0:
                        from hal.drivers.tracking.aim import _say

                        with look_debug.stage("speak_filler"):
                            _say("look_capturing")
                except Exception as e:
                    logger.warning("[realtime] look: aim raised, capturing anyway: %s", e)

            with look_debug.stage("capture"):
                frame = self._capture_frame(_capture_settle_s(res))
        if frame is None:
            logger.warning("[realtime] look: no camera frame available")
            look_debug.abandon("no_camera_frame")
            self._agent.send(
                [
                    FunctionCallResultInput(
                        call_id=output.call_id,
                        output='{"error": "camera unavailable"}',
                    )
                ]
            )
            return False

        # Legacy providers must resolve the tool call before the frame: Gemini drops realtime
        # input while a call is unanswered. trigger_response=True is the only ack that clears it.
        # The frame is still sent when the aim failed; the model is told `found_user`.
        found_user = bool(res is None or getattr(res, "aimed", False)
                          or getattr(res, "reason", "") != "subject not found")
        continue_interaction = getattr(self._agent, "supports_look_continuation", False) is True
        ack: dict[str, Any] = {"result": "frame incoming; wait for the image"}
        if continue_interaction:
            ack["result"] = "Captured image attached; answer using this image."
        if not found_user:
            ack["found_user"] = False
            ack["note"] = (
                "I could not find you in view. This frame is wherever the "
                "camera was already pointing, so say you cannot see them "
                "rather than describing it as if they had shown you something."
            )
        with look_debug.stage("ack_tool_call"):
            self._agent.send(
                [
                    FunctionCallResultInput(
                        call_id=output.call_id,
                        output=json.dumps(ack),
                        trigger_response=True,
                        image=frame if continue_interaction else None,
                    )
                ]
            )
        if not continue_interaction:
            with look_debug.stage("send_image"):
                self._agent.send([ImageInput(image=frame)])
        self._looked_this_turn = True
        # Stamp the SEND, not the call entry (the aim can take seconds).
        self._last_look_sent_monotonic = time.monotonic()
        # The replayed turn (frame + re-committed audio) inherits the same
        # risk of a long silent think over the frame; extend its watchdog too.
        # receive() clears the override when the replayed turn ends.
        self._agent.extend_recv_timeout(config.REALTIME_LOOK_RECV_TIMEOUT_S)
        # Persist the same frame so a delegate/fallback reuses it (see turn_dispatch).
        import hal.app_state as state

        with look_debug.stage("persist"):
            saved_path: str | None = self._persist_look_frame(frame)
        look_debug.note_capture(saved_path)
        # Attached to the turn message as a [snapshot: ...] marker by turn_dispatch.
        if config.LOOK_MONITOR_ENABLED:
            try:
                from hal.realtime.look_monitor import persist_for_monitor

                state.realtime_look_monitor_path = persist_for_monitor(saved_path)
            except Exception as e:
                logger.debug("[realtime] look: monitor copy skipped: %s", e)
        logger.info(
            "[realtime] look: captured frame %s in %.0fms → %s — %s",
            getattr(frame, "shape", "?"),
            (time.monotonic() - now) * 1000,
            saved_path or "(persist failed)",
            "continuing asynchronous interaction" if continue_interaction else "replaying turn",
        )
        # Extended Thinking keeps the tool interaction alive; replay would interrupt it.
        return not continue_interaction

    @staticmethod
    def _capture_frame(settle_s: float = 0.3) -> Any:
        """Latest camera frame (BGR) downscaled for cost, or None; waits for servos to be still."""
        try:
            import cv2

            import hal.app_state as state
            from hal.drivers.camera.video_capture_device import capture_still
        except Exception:
            return None
        from hal import privacy
        cap = getattr(state, "camera_capture", None)
        if cap is None or privacy.camera_muted:
            return None
        was_disabled: bool = bool(getattr(state, "_camera_disabled", False))
        try:
            if was_disabled:
                cap.start()
            try:
                frame: Any = capture_still(
                    cap,
                    getattr(state, "animation_service", None),
                    settle_s=settle_s,
                    # A camera woken from disabled can take over a second to deliver its first frame.
                    timeout_s=2.0,
                )
            finally:
                if was_disabled:
                    cap.stop()
            if frame is None:
                return None
            if privacy.camera_muted:
                return None
            max_w: int = config.REALTIME_GEMINI_VISION_MAX_WIDTH
            h, w = frame.shape[:2]
            if max_w and w > max_w:
                scale: float = max_w / float(w)
                frame = cv2.resize(
                    frame,
                    (max(1, int(w * scale)), max(1, int(h * scale))),
                    interpolation=cv2.INTER_AREA,
                )
            return frame
        except Exception:
            logger.exception("[realtime] look: frame capture failed")
            return None

    @staticmethod
    def _persist_look_frame(frame: Any) -> str | None:
        """Save the look frame and record it in app_state for delegate/fallback turns; returns the path."""
        try:
            import os
            import time as _time

            import cv2

            import hal.app_state as state

            os.makedirs(state._SNAPSHOT_DIR, exist_ok=True)
            path: str = os.path.join(
                state._SNAPSHOT_DIR, f"look_{int(_time.time() * 1000)}.jpg"
            )
            if not cv2.imwrite(path, frame):
                return None
            # Drop the previous look frame so they don't pile up.
            prev = getattr(state, "realtime_look_frame_path", None)
            if prev and prev != path and os.path.basename(prev).startswith("look_"):
                try:
                    os.remove(prev)
                except OSError:
                    pass
            state.realtime_look_frame_path = path
            state.realtime_look_frame_ts = time.monotonic()
            return path
        except Exception:
            logger.exception("[realtime] look: persist frame failed")
            return None

    def send_text(self, text: str) -> None:
        """Send a text message to the agent as context (non-blocking)."""
        if (
            config.REALTIME_PROVIDER.strip().lower() == "gemini"
            and gemini_needs_idle_workaround()
        ):
            # 2.5 native-audio can close with WS 1011 after clientContent turns; drop text there (INFO:
            # the symptom is the model addressing the wrong person).
            logger.info(
                "[realtime->model] DROPPED (gemini native-audio wire-shape guard): %r",
                text[:200],
            )
            return
        if self._agent is None:
            logger.info("[realtime->model] DROPPED (no agent session): %r", text[:200])
            return
        logger.info("[realtime->model] TEXT: %r", text[:300])
        self._agent.send([TextInput(text=text)])

    def save_turn(self, user_text: str, agent_text: str) -> None:
        """Save a conversation turn to realtime memory."""
        self._context.add_turn(user_text, agent_text)

    def save_main_handoff(self, user_text: str) -> None:
        """Persist a user turn that the main agent will answer (survives a session recycle)."""
        self.save_turn(
            user_text=user_text,
            agent_text="[This request was handed to the main agent; its spoken reply follows.]",
        )

    def save_main_agent_reply_fragment(self, text: str) -> None:
        """Persist a spoken main-agent reply fragment for future realtime sessions."""
        if text.strip():
            self.save_turn(user_text="[Main agent reply]", agent_text=text)

    def send_function_result(self, call_id: str, output: str) -> None:
        """Send a function call result back to the model."""
        if self._agent is not None:
            self._agent.send([FunctionCallResultInput(call_id=call_id, output=output)])
