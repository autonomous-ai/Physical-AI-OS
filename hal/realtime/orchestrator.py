"""Realtime orchestrator — manages voice agent lifecycle and turn processing.

Exposes a simple interface to the voice pipeline:
  - append_audio(frame) — queues audio to the model (non-blocking)
  - commit_audio() — queues commit signal (non-blocking)
  - stream_output() — yields outputs one by one as they arrive

The caller (voice_service) drives the orchestrator:
  1. Stream mic frames via append_audio()
  2. Call commit_audio() when done
  3. Iterate stream_output():
     - Yields AudioOutput / TextOutput / FunctionCallOutput chunks
     - Yields DelegateSignal if model called delegate_to_main (then stops)
     - Yields RejectSignal if model explicitly rejects a non-user turn (then stops)
"""

import json
import logging
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
from hal.realtime.models.output import ExecutionOutput, InterruptedOutput, UserSpeechOutput
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
# How often the idle watchdog checks whether the session should be parked. The
# park threshold has tens of seconds of margin below the server's idle cut, so a
# coarse poll is enough and keeps the thread cheap.
IDLE_PARK_POLL_S: float = 5.0
# How long prepare_turn() waits for a prewarm() resume already in flight. Above
# a normal handshake (~1-2s) so a prewarm is never abandoned for a second
# synchronous connect; bounded so a hung proxy still falls back to the main agent.
PREWARM_JOIN_TIMEOUT_S: float = 4.0
# Upper bound on how long prepare_turn()'s in-flight marker suppresses parking.
# voice_service can prepare a turn and then abandon it (wake word never
# confirmed) without ever calling stream_output, which would otherwise pin the
# marker True forever and disable parking until the next completed turn.
TURN_IN_FLIGHT_MAX_S: float = 120.0
# How long a user turn waits for a preempted announcement to finish draining
# its response before committing, and how long that drain may take. Past it the
# user turn goes ahead; flush_output() drops whatever the drain left behind.
ANNOUNCE_DRAIN_S: float = 3.0
# Silent-turn watchdog for an announcement. Nobody is waiting on it the way a
# user waits on a reply, and the pipecat relay was measured at 11.6 s to first
# token on a cold 12.5 k-token context (lamp-ee17, 2026-09-25) — past the
# default receive timeout, which then spoke the fallback instead.
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
# Conversational emotions the agent may set to match its own spoken tone.
# Excludes system/background states (idle, listening, sleepy, scan, nod, music_*)
# which are driven by the device, not the agent.
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

# Capture settle. 0.3s suits the tracker's small corrections, but an aim that
# exits on its deadline does so right after a large swing, and a lamp arm is
# still ringing well past 300ms — which is why timed-out looks came back blurred
# while centred ones were sharp (device-observed 2026-08-19).
#
# The ceiling is deliberately tight: this delay is paid before the user gets an
# answer, so a sharper frame is not worth much latency. A 30 deg swing buys
# 200ms extra, and nothing buys more.
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

# No parameters: the model just signals intent to look; the device grabs the
# current frame. Kept empty so the call is as cheap/fast as possible.
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
    """True when a camera is wired up — the device's `vision` capability at
    runtime. app_state.camera_capture is only set by server.py when ROBOT.md
    declares `vision`, so this is the single source of truth for "can look".
    Lazy import keeps app_state out of this module's import graph."""
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
    """True when the client-side `web_search` tool should be registered: the
    provider is the on-device Pipecat pipeline (the only one without a hosted
    search of its own — Gemini grounds, GPT-Live's backend searches) and the
    flag is on. Read at construction, like the vision gate."""
    return (
        config.REALTIME_PIPECAT_WEB_SEARCH
        and config.REALTIME_PROVIDER.strip().lower() == "pipecat_v1"
    )


class RealtimeOrchestrator:
    """Manages a single realtime voice agent session.

    Automatically registers the delegate_to_main tool so the model
    can signal that the user's request should be handled by the main
    flow (device → OpenClaw).
    """

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
        # Returns the Gemini Live voice to use instead of config (the Gemini TTS
        # voice, see tts/gemini.py native_voice), or None for the config voice.
        self._voice_override: Callable[[], str | None] | None = voice_override
        # VoiceService's STT provider, handed to providers that run STT
        # themselves (pipecat_v1) so the pipeline transcribes on the same
        # relay, key, model and boost terms as the turn-based path. None →
        # such a provider builds its own from config.
        self._stt_provider: Any = stt_provider
        # express_emotion is registered ONLY when the device declares the
        # `expression` capability (ROBOT.md → expression: { routes: [emotion] }).
        # A device with no face (e.g. mic+speaker only) never sees the tool, so
        # the model can't call it and nothing fires.
        self._expression_enabled: bool = enable_expression
        tools: list[dict[str, Any]] = [DELEGATE_TOOL]
        # Keep the AI rejection policy behind one explicit runtime flag. When
        # disabled, a silent model turn follows the existing main-agent fallback.
        if config.REALTIME_AI_REJECT_FILTER:
            tools.append(REJECT_TURN_TOOL)
        # Live mode only: on the turn-based path there is no open session to
        # hang up, so offering it there would just be a tool the model can
        # call to no effect.
        if config.LIVE_MODE:
            tools.append(END_CALL_TOOL)
        if enable_expression:
            tools.append(EMOTION_TOOL)
        # `look` (in-session vision) is registered only when ALL hold: a camera is
        # present, the config flag is on (REALTIME_GEMINI_VISION), and the provider
        # is Gemini (the image-injection → continue-turn flow is implemented +
        # tested for Gemini Live; OpenAI keeps delegating). Otherwise visual
        # questions fall back to delegate_to_main.
        #
        # Camera presence IS the device's `vision` capability at runtime: server.py
        # only creates app_state.camera_capture when ROBOT.md declares `vision`
        # (the `camera` route mounts). Reusing that one signal — the same one
        # /health and _capture_frame read — means no extra capability plumbing and
        # it's correct for EVERY construction path (auto-start and /voice/start).
        self._vision_enabled: bool = (
            _camera_present()
            and config.REALTIME_GEMINI_VISION
            and config.REALTIME_PROVIDER.strip().lower() == "gemini"
        )
        if self._vision_enabled:
            tools.append(LOOK_TOOL)
        # `web_search` (grounded public lookups) is registered only for the
        # pipecat provider, whose relay LLM has no hosted search; Gemini and
        # GPT-Live ground on their own side. See _handle_web_search_call.
        self._web_search_enabled: bool = _web_search_available()
        if self._web_search_enabled:
            tools.append(WEB_SEARCH_TOOL)
        self._tools: list[dict[str, Any]] = tools + (extra_tools or [])
        # `look` cost guards: at most ONE image sent per turn, and no fresh image
        # within VISION_MIN_INTERVAL_S of the last send (the recent frame is still
        # in context, so repeat looks reuse it instead of re-billing image tokens).
        self._looked_this_turn: bool = False
        self._last_look_sent_monotonic: float = 0.0
        self._agent: VoiceAgentBase | None = None
        self._started: threading.Event = threading.Event()
        # A provider can be unreachable while HAL starts (network/DNS outage,
        # temporary quota error, upstream maintenance). VoiceAgentBase can only
        # reconnect after its send/receive loops exist, so a failure in its first
        # connect() needs an orchestrator-owned retry loop.
        self._connect_retry_stop: threading.Event = threading.Event()
        self._connect_retry_thread: threading.Thread | None = None
        # Traceback of the failed initial connect, held until the retry loop
        # decides whether it mattered: dropped on a recovery, logged at ERROR
        # once the first retry also fails. See start() / _connect_retry_loop().
        self._initial_connect_exc: BaseException | None = None
        # Idle parking: the transport is closed while nobody is talking so the
        # server never has to close it (see _idle_park_loop / config
        # REALTIME_GEMINI_IDLE_PARK_S). `available` stays True while parked --
        # prepare_turn() reconnects on demand.
        self._idle_parked: bool = False
        self._idle_park_stop: threading.Event = threading.Event()
        self._idle_park_thread: threading.Thread | None = None
        self._park_resume_failed: bool = False
        self._turn_in_flight: bool = False
        self._turn_started_monotonic: float = 0.0
        # Last moment this session saw turn activity (prepare/audio/text/reply
        # end). Seeded at connect so a session idle since boot parks too.
        self._last_activity_monotonic: float = 0.0
        # Serializes start/stop with session swaps. A reconnect that completes
        # after stop must never publish a live agent back into the stopped HAL.
        self._lifecycle_lock: threading.Lock = threading.Lock()
        self._consecutive_silent: int = 0  # zombie-session guard (see stream_output)
        # True while a live (full-duplex) session owns the mic. Suppresses every
        # post-turn session recycle — see stream_output.
        self._live_active: bool = False
        # Idle session recycle (cost): when a turn arrives after a long silence,
        # rebuild the session AFTER that turn so the next turn drops the context
        # the provider re-bills on a long-lived session. See _maybe_idle_reset.
        self._last_turn_monotonic: float = 0.0  # end-of-turn timestamp; 0 = none yet
        # When the CURRENT session connected; 0 = none yet. Idle checks measure
        # from the later of this and the last turn: a session that connected a
        # second ago (privacy-switch unmute, reconnect) is not idle, however
        # long ago the last turn ended.
        self._session_connected_monotonic: float = 0.0
        self._idle_reset_pending: bool = False
        # Set when Gemini proactively rebuilt before the current turn after a
        # long idle. That session is already fresh, so the generic post-turn
        # idle cost recycle must not immediately create a second handshake.
        self._skip_post_idle_recycle: bool = False
        self._turns_since_recycle: int = 0  # turn-cap recycle counter (cost)
        self._rebuild_lock: threading.Lock = threading.Lock()  # guards _force_rebuild
        # Set whenever no rebuild is in flight. Voice capture uses this to keep
        # collecting a turn that starts immediately after a noise-drop, rather
        # than losing its opening frames while Gemini establishes a clean session.
        self._rebuild_done: threading.Event = threading.Event()
        self._rebuild_done.set()
        # Device-initiated announcements (see announce()). A user capture
        # preempts one through _announce_stop; its commit waits on
        # _announce_idle so the two never read the output queue together.
        self._announce_stop: threading.Event | None = None
        self._announce_idle: threading.Event = threading.Event()
        self._announce_idle.set()
        summarizer: RealtimeSummarizer | None = None
        if config.REALTIME_SUMMARIZER_ENABLED:
            try:
                summarizer = RealtimeSummarizer()
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
        # Require the agent to be actually CONNECTED, not just constructed. On a
        # Gemini usage-limit close (code 4029) or any failed reconnect the agent
        # object survives but its session is dead; without the _agent.available
        # check, voice_service still routes the turn to realtime, commits, and
        # blocks ~15s in receive() before falling back. Gating on the live
        # connection makes those turns skip realtime and go straight to the main
        # agent immediately (and auto-resume once the limit clears / reconnect wins).
        #
        # Also report unavailable WHILE a session rebuild is in flight. _force_rebuild
        # keeps the old agent serving until the new one connects (~1s) then swaps and
        # disconnects the old — so a turn that dispatches during that window commits
        # its audio to the about-to-be-discarded old session and dies mid-turn with
        # WS 1000 (the "async rebuild raced the next turn" failure). Gating on the
        # rebuild lock routes such turns cleanly to the main agent for that ~1s
        # instead, eliminating that whole class of mid-turn closes.
        # A PARKED session is reported available on purpose: its transport is
        # closed, but prepare_turn() reconnects a fresh one synchronously before
        # any audio is streamed. Reporting unavailable instead would route every
        # post-idle turn to the main agent — the exact regression this guard was
        # written to prevent, only self-inflicted.
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
        """Wait briefly for an already-started rebuild without starting one.

        This is intentionally only a bounded wait. A voice turn that starts
        during a background noise-drop rebuild keeps its audio locally first;
        when the replacement session is ready, the caller flushes that complete
        audio buffer in order. A failed/slow reconnect falls back to the main
        agent instead of holding a finished user turn indefinitely.
        """
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
        """Build (not connect) a fresh agent for the provider. Shared by start()
        and the zombie rebuild so both create the agent identically."""
        if provider == "gemini":
            from hal.realtime.voice_agent.gemini_live import GeminiLiveAgent

            return GeminiLiveAgent(
                config=GeminiConfig(
                    instructions=instructions, voice=GeminiVoice(self._gemini_voice()),
                ),
                tools=self._tools,
            )
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
            # Images live in the session context — a fresh session has none, so
            # the look reuse-guard must not claim "recent frame still in context"
            # (it would ack the model with no image there → silent turn).
            self._looked_this_turn = False
            self._last_look_sent_monotonic = 0.0
            logger.info("[realtime] Fresh session connected before turn (%s)", reason)
            return True
        except Exception:
            logger.exception("[realtime] Pre-turn session rebuild failed")
            # A failed connect can still have allocated a provider transport or
            # event-loop thread. Retry loops must tear it down before creating
            # another fresh agent, otherwise a persistent outage leaks workers.
            if new is not None:
                try:
                    new.disconnect()
                except Exception:
                    logger.exception("[realtime] failed replacement disconnect failed")
            # A dropped Manual-VAD activity must never become the next user
            # turn when its clean replacement could not connect. Mark it dead
            # so voice_service falls back with the STT transcript instead.
            if discard_old_on_failure:
                self._agent = None
            return False
        finally:
            self._finish_rebuild()
            if old is not None and old is not self._agent:
                self._disconnect_in_background(old, reason)

    @staticmethod
    def _disconnect_in_background(agent, reason: str) -> None:
        """Tear the replaced session down without holding up the turn.

        The new session is already live by the time we get here — nothing waits
        on the old one being cleanly closed. Doing it inline did make the turn
        wait: `disconnect()` awaits the provider's `aclose()` and then joins the
        IO thread, measured at 0.79s between the fresh Gemini session opening
        and this turn's [TURN CONTEXT] going out (lamp-0c89, 03/09/2026) — dead
        time inserted between the user finishing a sentence and the model
        hearing it. Failures stay logged, they just no longer cost the user a
        second of silence.
        """
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
            # Thread creation is the one failure worth absorbing inline: leaking
            # the socket is worse than the delay we were avoiding.
            logger.warning("[realtime] could not background the old-session close; closing inline")
            _close()

    def _rebuild_now(
        self,
        reason: str,
        cancel_event: threading.Event | None = None,
        discard_old_on_failure: bool = False,
    ) -> bool:
        """Synchronously swap in a fresh session before audio is streamed.

        Used for Gemini idle-gap recovery: if the proxy/SDK may have dropped an
        idle socket, reconnecting after commit is too late because this turn's
        audio has already been sent to the dead session.
        """
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
        """Drop a turn whose audio already streamed into the session.

        Manual-VAD Gemini has no "discard" primitive: dropped-turn audio sits
        in the still-open activity and gets billed with (and can confuse) the
        NEXT committed turn. Closing with activityEnd instead would make the
        model answer the noise — the exact reply REQUIRE_TRANSCRIPT exists to
        prevent. The only clean exit is a fresh session. Start that handshake
        in the background: a user who starts speaking immediately is buffered
        by voice_service and then flushed to the new session in full. No-op
        when no activity is open (auto-VAD providers, or no audio streamed yet).
        """
        if self._agent is None or not getattr(self._agent, "_activity_started", False):
            return False
        return self._rebuild_in_background(
            reason, "rt-noise-rebuild", discard_old_on_failure=True
        )

    def recover_session(self, reason: str) -> bool:
        """Reconnect a FRESH session synchronously for a mid-turn 1011 replay.

        Used by run_realtime_turn when a turn produced no output (likely WS 1011
        from a proxy-dropped idle session): swap in a fresh agent so the caller
        can immediately replay the captured turn audio onto an active session.
        Returns True if a fresh session is ready.
        """
        return self._rebuild_now(reason)

    def set_live_active(self, active: bool) -> None:
        """Mark that a live (full-duplex) session is streaming into this agent.

        The post-turn recycles in stream_output all assume a turn is a discrete
        unit with a gap after it. Inside a live session none of that holds: the
        mic keeps appending across every model reply, so a rebuild swaps the
        agent out from under an open uplink. Recycling resumes when it clears.
        """
        self._live_active = active
        if not active:
            # The session that just ended may well have earned a recycle while
            # it ran. Start the next one from a clean count rather than firing
            # a rebuild on the first turn after it for reasons that belong to
            # the live session.
            self._consecutive_silent = 0
            self._turns_since_recycle = 0
            self._idle_reset_pending = False

    def prewarm(self) -> bool:
        """Resume a parked session in the background as soon as speech starts.

        Wake-word turns defer every realtime I/O until the STT final confirms
        the phrase, so the parked-session resume (~2s handshake, device-measured
        lamp-4ace 22/09/2026) otherwise runs serially AFTER capture. Starting it
        here overlaps the handshake with the user's own sentence; prepare_turn()
        joins it. No-op unless parked. A turn that is then never dispatched just
        leaves an idle session the park watchdog closes again.
        """
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
        # A new capture starts a new logical turn. Clear any marker left by a
        # capture that was dropped before it reached stream_output().
        self._skip_post_idle_recycle = False
        self._last_activity_monotonic = time.monotonic()
        self._turn_started_monotonic = time.monotonic()
        # Mark the turn BEFORE looking for an announcement: announce() registers
        # its stop event and then checks this flag, so one of the two always
        # sees the other.
        self._turn_in_flight = True
        # The user always wins over a device announcement: stop it now, while
        # they are still speaking, so it has drained before their commit.
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
            # A prewarm() started the resume while the user was still speaking;
            # join it instead of falling back for "rebuild in flight".
            if self.rebuilding:
                self._rebuild_done.wait(timeout=PREWARM_JOIN_TIMEOUT_S)
                if not self._idle_parked:
                    self._skip_post_idle_recycle = True
                    return
            # The idle watchdog closed the transport; connect a fresh session
            # now, before any audio is streamed. Identical to the pre-turn idle
            # recycle below (a parked session is idle by definition), so the
            # post-turn idle recycle is skipped the same way.
            if self._rebuild_now("idle-park-resume"):
                self._skip_post_idle_recycle = True
                return
            # Stay parked so the next turn retries the resume, but report
            # unavailable meanwhile: the transport really is closed, so this
            # turn must go to the main agent instead of into a dead session.
            self._park_resume_failed = True
            logger.warning(
                "[realtime] Could not resume parked session — falling back for this turn"
            )
            return
        # An unanswered Gemini tool call makes the entire Live session
        # non-reusable: audio, turn-context text, video, and manual-VAD
        # activityEnd can all be rejected with 1008. Fire-and-forget expression
        # calls intentionally skip their Gemini acknowledgement to avoid a
        # duplicate reply, so replace that session before the next capture.
        # A handoff also quarantines the session: late provider output
        # must not start a response/grace belonging to the next user capture.
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
        # The voice is fixed at connect, so a TTS voice/provider change made
        # since this session opened needs a fresh one before this turn speaks.
        if provider == "gemini" and agent is not None:
            current = getattr(getattr(agent, "_config", None), "voice", None)
            wanted = self._gemini_voice()
            if current is not None and current != wanted:
                logger.info("[realtime] Gemini voice %s -> %s — rebuilding session", current, wanted)
                if self._rebuild_now("gemini-voice-change", discard_old_on_failure=True):
                    self._skip_post_idle_recycle = True
                return
        # NOT gated on gemini_needs_idle_workaround(). That predicate asks whether
        # the MODEL has the native-audio idle-resume bug, but an idle session dying
        # is a session-lifecycle fact, not a model one: Gemini closes a session it
        # receives nothing on, whichever model is behind it. Measured 2026-08-21 on
        # gemini-3.1-flash-live-preview (lamp-0c89 and intern-v2): WS 1008 "The
        # operation was aborted" after 86-185s with nothing sent to the model, and
        # 107 of 113 such closes landed on sessions that had served ZERO turns.
        # Gating this on the model left 3.1 with no protection at all.
        if provider != "gemini":
            return
        threshold = config.REALTIME_GEMINI_PRE_TURN_RECYCLE_S
        if threshold <= 0 or self._last_turn_monotonic <= 0.0:
            return
        # Session idle, not user idle: after an unmute the session is seconds
        # old even when the last turn is minutes old. Rebuilding it anyway cost
        # a ~10s reconnect the turn fell back through (device 2026-09-23).
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

    # --- Idle parking ---

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
        """Close the transport once the session has gone unused long enough.

        The server kills an idle Gemini session itself (WS 1008 "The operation
        was aborted"), which the backend logs as an error and alerts on. Closing
        first keeps that close out of the backend's logs entirely. Nothing is
        lost by it: any turn arriving after this much silence would have been
        given a fresh session by the pre-turn recycle anyway.
        """
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

        Gemini: close before the server's own idle kill pages the backend.
        GPT-Live: close because the session is billed per minute while it sits
        open. OpenAI Realtime bills per token, so an idle session is free and is
        left to the server's own timeout. Pipecat v1 has no vendor session at
        all (the pipeline is local; its STT socket only lives during a turn or
        a live call), so it is never parked.
        """
        provider: str = config.REALTIME_PROVIDER.strip().lower()
        if provider == "gemini":
            return config.REALTIME_GEMINI_IDLE_PARK_S
        if provider == "gptlive":
            return config.REALTIME_GPTLIVE_IDLE_PARK_S
        return 0.0

    def _park_idle_session(self, idle_s: float) -> None:
        """Disconnect the current session and mark it resumable on the next turn.

        Held under the rebuild reservation so no turn dispatches into the session
        while it is closing — voice_service buffers a capture that starts during
        a rebuild and flushes it into the replacement (see `rebuilding`).
        """
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
        """Recover a zombie session by building a BRAND-NEW agent and swapping it
        in, then discarding the old one.

        We do NOT reconnect the existing agent: its recv thread is wedged inside
        `async for session.receive()` on a dead socket, and closing that session
        does not reliably unblock it — so an in-place reconnect never fires (the
        send loop only reconnects on a queued input, but inputs are gated on
        `available`, which is False once we clear `_connected` → deadlock). A
        fresh agent has its own loop / threads / session, sidestepping the wedge
        entirely (this is what a HAL restart does, minus the process). Runs on a
        daemon thread so the voice turn doesn't block on connect; a lock prevents
        overlapping rebuilds while one is in flight."""
        self._rebuild_in_background("zombie-recovery", "rt-zombie-rebuild")

    def _start_connect_retry_loop(self) -> None:
        """Reconnect after the initial provider connection failed.

        The initial ``VoiceAgentBase.connect()`` fails before it starts the
        provider's send/receive loops, so no provider-level retry can occur.
        Keep retries off the voice thread and back off boundedly; voice turns
        continue through the main-agent fallback until a session is available.
        """
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
                # Recovered — the original failure was transient, so its
                # traceback is noise and is dropped rather than logged.
                self._initial_connect_exc = None
                logger.info("[realtime] Initial connection recovered automatically")
                return
            if self._initial_connect_exc is not None:
                # Retry did not clear it: now the initial failure is worth a
                # full ERROR. Logged once, then the loop stays at WARNING.
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
            # A single failed handshake is usually a transient upstream hiccup
            # that the retry loop clears within seconds, so it is not an ERROR
            # yet — one WARNING line here, traceback kept for _connect_retry_loop
            # to log at ERROR only if the retry does not recover.
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

        # Catch up on any unsummarized memory from a previous (possibly crashed)
        # session in the background. This is an LLM call and is NOT needed to serve
        # turns — running it before connect would keep `available` False for seconds
        # after a restart, leaking early turns ("hello") to the main agent. The
        # summarizer is concurrency-safe (keeps entries appended during summarization),
        # so it is safe to run alongside the live session.
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
        # Summarize remaining memory before shutdown
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
        """Detect a long idle gap before this turn and arm a post-turn session
        recycle. We measure end-of-previous-turn → start-of-this-turn (the silence
        gap), and only ARM here — the rebuild itself fires at the END of
        stream_output so it never swaps self._agent while this turn's audio
        (already appended to the current session) is mid-commit. Cost rationale:
        a session that has been chatting for a while re-bills its accumulated
        context every turn; a turn that follows a long pause is effectively a new
        conversation, so recycling then drops that context for ~free (long-term
        continuity is preserved via the summary the rebuild reloads)."""
        if self._skip_post_idle_recycle:
            # prepare_turn() just connected a fresh Gemini session for this
            # idle gap. Keep it after the reply; the next turn already has the
            # low-context session, so a second recycle only churns handshakes.
            return

        reset_s = config.REALTIME_SESSION_IDLE_RESET_S
        if reset_s <= 0 or self._last_turn_monotonic <= 0.0:
            return
        # A session connected after the last turn holds no accumulated
        # context to drop, so it is measured from its connect too.
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
        """Discard buffered outputs from a prior turn before committing a new one.

        Prevents a stale response (from an earlier, possibly noise-triggered turn)
        being read and spoken on the current turn — see VoiceAgentBase.flush_output.
        """
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
        """Yield outputs from the model one by one as they arrive.

        Yields:
          - AudioOutput / TextOutput / FunctionCallOutput as they stream in
          - DelegateSignal if model called delegate_to_main (then stops)
          - RejectSignal if model explicitly rejected the turn (then stops)
          - LookReplaySignal if model called look and a fresh frame was sent —
            the caller must re-append the turn's audio and commit again so the
            frame joins the replayed turn (then stops)

        The generator returns (StopIteration) when the model's turn is done.
        """
        self.execution_completed = False
        self.execution_turn_id = ""
        if turn is not None:
            self._validate_audio_turn(turn)
        if self._agent is None:
            return
        execution_agent = self._agent

        self._looked_this_turn = False  # reset the per-turn `look` image-send guard
        produced = False  # did this turn yield any real output (vs stay silent)?
        rejected = False
        replay_pending = False  # look-replay signalled — the turn continues
        receive_kwargs: dict[str, Any] = {"stop_on_done": True}
        if stop_event is not None:
            receive_kwargs["stop_event"] = stop_event
        for output in execution_agent.receive(**receive_kwargs):
            if stop_event is not None and stop_event.is_set():
                return
            if turn is not None:
                self._validate_audio_turn(turn)
            if isinstance(output, MainAgentFallbackOutput):
                # This is a local fail-safe, not a provider function call: there
                # is no call ID to acknowledge. Preserve the user's request even
                # when a filler has already reached the speaker.
                produced = True
                execution_agent.end_turn()
                yield DelegateSignal(
                    message=output.transcript,
                    transcript=output.transcript,
                    user_turn_id=output.user_turn_id,
                    handoff_context=output.handoff_context,
                )
                break
            if isinstance(output, ExecutionOutput):
                yield output
                continue
            if isinstance(output, InterruptedOutput) and output.reason == "server_interrupt":
                # This new signal observes the provider's cancellation. It must
                # not change the existing tool/routing state of this generator.
                yield output
                continue
            if isinstance(output, UserSpeechOutput):
                # Input observations are not an answer: they must not prevent
                # reject_turn or change the look/emotion tool's spoken state.
                yield output
                continue
            if (
                isinstance(output, FunctionCallOutput)
                and output.name == LOOK_TOOL_NAME
            ):
                # A find is a servo search, never a look — delegate it before
                # any aim or capture (see _redirect_find_look).
                redirect = self._redirect_find_look(output)
                if redirect is not None:
                    produced = True
                    yield redirect
                    break
                # Fresh frame sent → the turn must be REPLAYED so the frame
                # (queued by the Live API for the next turn) joins the answer —
                # see _handle_look_call. Mirror the delegate flow: end_turn so
                # the replay's commit isn't gated on a turn_complete that never
                # comes, signal the turn driver, stop this turn.
                if self._handle_look_call(output):
                    produced = True
                    replay_pending = True
                    # Unblock the replay's commit (no turn_complete follows a
                    # tool-call-only turn) and arm receive() to swallow the
                    # cancelled turn's LATE turn_complete, which otherwise ends
                    # the replayed turn empty ~300ms in.
                    self._agent.end_turn()
                    self._agent.skip_next_turn_done()
                    yield LookReplaySignal()
                    break
                # Reused/absent frame → the tool was acked; the turn continues
                # and the spoken answer is yielded by the branches below.
                continue
            if (
                isinstance(output, FunctionCallOutput)
                and output.name == WEB_SEARCH_TOOL_NAME
            ):
                # Blocking lookup (~4 s), answered with trigger_response=True:
                # the model's follow-up reply is the spoken answer and is
                # yielded by the branches below. Counts as produced — a turn
                # that searched is neither silent nor rejectable.
                self._handle_web_search_call(output)
                produced = True
                continue
            if (
                isinstance(output, FunctionCallOutput)
                and output.name == EMOTION_TOOL_NAME
            ):
                # `produced` decides whether the ack is safe to send — see
                # _handle_emotion_call.
                self._handle_emotion_call(output, spoken=produced)
                continue
            if (
                isinstance(output, FunctionCallOutput)
                and output.name == END_CALL_TOOL_NAME
            ):
                logger.info("[realtime] Model asked to end the conversation")
                # Acknowledge with trigger_response=True, unlike the
                # fire-and-forget expression tool: the model must be free to
                # finish speaking its farewell, and an UNacknowledged call
                # makes Gemini reject every later client input (including
                # audio) for the rest of the session. The session is ending
                # anyway, so the usual "continues the turn" cost does not
                # apply here.
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
                # LIVE output can be queued before the routing tool arrives.
                # Honor explicit rejection so the consumer can cancel that turn
                # before playback. Preserve the manual-turn late-call policy.
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
                    # End rather than letting an acknowledgement generate a
                    # second response after the model has already spoken.
                    self._agent.end_turn()
                    break
                rejected = True
                logger.info("[realtime] Model explicitly rejected this turn")
                # Acknowledge like delegation so Gemini does not leave a pending
                # tool call that poisons the next manual-VAD activity.
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
                self._agent.send(
                    [
                        FunctionCallResultInput(
                            call_id=output.call_id,
                            output='{"result": "delegated"}',
                        )
                    ]
                )
                produced = True
                # Mark the turn done so the NEXT turn's commit isn't gated on a
                # turn_complete that never comes after a tool call (Gemini manual
                # VAD waits up to 10s on _turn_done otherwise).
                self._agent.end_turn()
                yield DelegateSignal(
                    message=delegate_msg, transcript=output.user_transcript,
                    user_turn_id=output.user_turn_id,
                    handoff_context=output.handoff_context,
                )
                # Stop the turn here — once the model has delegated, it has nothing
                # more to say, and waiting for turn_complete just blocks on the
                # receive() timeout (the model stays silent for the full
                # RECV_QUEUE_TIMEOUT, ~15s) before we forward to the main flow. We
                # already sent the function result above; the dangling open turn is
                # dropped by the next turn's flush_output(). Forward immediately.
                break
            produced = True
            yield output

        if turn is not None:
            self._validate_audio_turn(turn)

        # Session cancellation is not a silent/completed model turn and must
        # not trigger lifecycle accounting or a post-turn rebuild.
        if stop_event is not None and stop_event.is_set():
            return

        # Capture the consumed terminal before any post-turn recycle swaps agents.
        self.execution_completed = (
            getattr(execution_agent, "execution_completed", False) is True
            and not replay_pending
            and not rejected
        )
        self.execution_turn_id = getattr(execution_agent, "execution_turn_id", "")

        # Look-replay pending: the logical turn CONTINUES (the caller is about
        # to re-commit this turn's audio on the SAME session). Don't stamp,
        # count, or recycle here — an idle/turn-cap recycle at this point swaps
        # in a fresh session and orphans the image that was just sent to the
        # old one (device-observed 2026-07-02: replay ran on a blank session,
        # the model had no frame and stayed silent → main-agent fallback). Any
        # armed recycle stays pending and fires after the replayed turn.
        if replay_pending:
            return

        # The post-turn portion of a Gemini pre-turn recycle is intentionally
        # skipped. Keep the marker through a look replay above because it is
        # still the same logical user turn.
        skip_post_idle_recycle = self._skip_post_idle_recycle
        self._skip_post_idle_recycle = False

        # Track consecutive silent turns for the zombie guard: a turn that
        # committed audio but yielded nothing. A long-lived Gemini session can
        # wedge (connected, accepts audio, never replies — no go_away/close), so
        # N consecutive silent turns means force a fresh session. Reset on any
        # real output so interspersed noise turns don't trip it.
        if produced:
            self._consecutive_silent = 0
        else:
            self._consecutive_silent += 1

        # Stamp end-of-turn (next turn's idle measurement) + count this turn.
        # Also releases the idle watchdog's in-flight hold (see _maybe_park_idle_session).
        self._last_turn_monotonic = time.monotonic()
        self._last_activity_monotonic = self._last_turn_monotonic
        self._turn_in_flight = False
        self._turns_since_recycle += 1

        # Recycle the session (rebuild → drop the context the provider re-bills
        # every turn) for any of three reasons, decided in ONE place so the
        # rebuild never double-fires and all state resets together. Done between
        # turns so it never swaps self._agent mid-turn; rebuild is async so the
        # NEXT turn uses the fresh session. Continuity survives via summary.md.
        #   - zombie:   N consecutive silent turns (wedged session)
        #   - idle:     this turn followed a long silence (see _mark_turn_start)
        #   - turn-cap: session handled enough turns that context grew large (cost)
        # NOTE: NO grounding-triggered recycle. Recycling after every Google-Search
        # turn churned the session and the async rebuild raced the next turn (common
        # when the user asks consecutive search questions) → frequent mid-turn WS 1000
        # closes. The snippet-eviction saving (~200t) wasn't worth it; turn-cap/idle
        # recycle already bounds accumulation. The race itself (rebuild swapping the
        # agent under a dispatching turn) is now also blocked by the available-gate
        # checking _rebuild_lock — see the `available` property.
        # Every reason below is wrong inside a live session:
        #   - zombie:   a quiet stretch produces no output and ends stream_output
        #               on the receive() timeout, so ZOMBIE_RECONNECT_AFTER of
        #               those (~24s of the user simply not talking) would force
        #               a reconnect mid-conversation.
        #   - turn-cap: swaps the session every SESSION_MAX_TURNS replies.
        #   - idle:     same swap, same open uplink.
        # A rebuild is also not survivable here the way it is between turns —
        # the mic pump keeps appending straight through the swap. Deferred, not
        # cancelled: set_live_active(False) resets the counters at hangup.
        # getattr: stream_output is exercised by tests that build partial
        # orchestrator state via object.__new__, same as _idle_parked above.
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

    # --- Device-initiated announcements ---

    @property
    def turn_in_flight(self) -> bool:
        """A user turn is between prepare_turn() and the end of its reply."""
        return bool(
            getattr(self, "_turn_in_flight", False)
            and time.monotonic() - getattr(self, "_turn_started_monotonic", 0.0)
            < TURN_IN_FLIGHT_MAX_S
        )

    def finish_capture(self) -> None:
        """HAL has finished processing a capture; none of its output is still read here.

        A delegated reply arrives later through TTS, which the announcer's own
        speaking gate covers. A preempting announcement, if any, has already
        stopped — prepare_turn() ran at the capture's start.
        """
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
        """Ready the session for announce(); False means render it without us.

        allow_resume=False refuses to reconnect a parked session (a progress
        line is not worth a handshake). Otherwise this resumes/replaces the
        session exactly as a user turn would, synchronously.
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
            # Manual-VAD Gemini with an activity no capture owns any more (a
            # capture that ended without a commit). Text sent into it would
            # split that activity, so the provider refuses the announcement;
            # replace the session as discard_open_activity does for noise.
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

        Call prepare_announcement() first. A user capture sets stop_event (via
        prepare_turn); the generator then stops yielding and drains the rest of
        this response so it cannot leak into the user's turn.
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
            # A preempted receive() keeps its watchdog override; the user's
            # turn must start from the default.
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
        """Answer the model's `web_search` call with a grounded result.

        Runs the lookup synchronously on this (turn output) thread: the
        pipeline's tool handler is parked on its future meanwhile, bounded by
        HAL_PIPECAT_TOOL_RESULT_TIMEOUT_S, and the turn-based path's dead-air
        filler + thinking cue already cover the wait audibly and visually.
        Every outcome is acknowledged with trigger_response=True — the model
        must speak either the answer or a short "couldn't check", and an
        unanswered call would only ever time out into the bridge's generic
        error. On failure the payload also points the model at
        delegate_to_main so the question still reaches the main agent, which
        is where it went before this tool existed.
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
        """Fire the device's emotion expression without blocking the spoken turn.

        Fire-and-forget: the HAL /emotion call runs in a daemon thread (parallel
        to the audio already streaming), and the function result is acknowledged
        with trigger_response=False so it does NOT spawn a second model response.
        Net added latency to speech is ~zero.
        """
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

        # Whether to ack depends on whether the model has spoken yet THIS turn.
        #
        # spoken=True — the reply is already out and the emotion was a side
        #   effect. Acking here makes Gemini continue the turn and re-speak the
        #   whole reply, double-billing TTS (device-observed 2026-06-29). Stay
        #   silent; the side effect has already run. This is the common path and
        #   its behaviour is unchanged.
        #
        # spoken=False — the tool call IS the model's entire generation so far.
        #   Gemini pauses generation until it gets a tool response, so skipping
        #   the ack deadlocks the turn: the model never speaks, the watchdog
        #   fires (8s, or 20s on a look turn) and the turn falls back to the main
        #   agent. Device-observed 2026-08-19 — every express_emotion turn that
        #   day died this way, look or not. Ack so generation resumes.
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
        """Drive the device face by calling the HAL emotion handler in-process.

        The realtime agent runs inside the HAL process, so we call the route
        handler directly instead of looping back over HTTP — no serialization,
        no network stack, lower latency. Runs in a daemon thread; logs the
        outcome (status ok / ignored) so device testing can confirm the emotion
        actually fired and measure how long it took.
        """
        started: float = time.monotonic()
        try:
            # Lazy import: the emotion handler pulls in app_state / LED / servo;
            # keep that out of the driver's module-load graph and avoid any cycle.
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
        """Hand a find/search request that reached `look` to the main agent.

        The prompt already says a find is a servo search, not a look, but the
        model can ignore it: "Find my mouse" went through `look` on 3.8
        extended-thinking, the frame was persisted, and the main agent then got
        the request with an unrelated pre-search [vision-image] (#481). Decided
        here, before aim and capture, so no frame is taken or attached. Mirrors
        the delegate_to_main branch: ack the call, end the turn, delegate.
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

        # No frame belongs to this request; drop any earlier one so the handoff
        # cannot carry a stale [vision-image].
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
        """Handle the model's `look` call. Returns True when a FRESH frame was
        sent and the turn must be REPLAYED (see LookReplaySignal), False when
        the turn should just continue (frame reused from context, or no camera).

        Extended Thinking sessions with observed interaction status continue
        their async tool interaction after the fresh image. They must not replay
        user activity, which would interrupt background work. Legacy sessions
        retain audio replay: those previously answered from the preceding image
        when given only a mid-turn frame and an acknowledgement.

        Reuse path (already looked this turn / within VISION_MIN_INTERVAL_S of
        the last send): the recent frame is genuinely in context — in the
        replay turn this is exactly how the model reads the fresh frame — so
        ack with trigger_response=True and let the turn continue.
        """
        if self._agent is None:
            return False
        from hal.drivers.tracking import look_debug

        look_debug.start()
        now: float = time.monotonic()
        # Cost guard: no NEW image within VISION_MIN_INTERVAL_S of the last send
        # or twice in one turn. Doubles as the replay-turn path: the replayed
        # turn re-triggers look, lands here, and the model answers from the
        # frame that entered context with the replay.
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
            # NOT a failure and NOT a separate look: this is the replayed turn
            # reading the frame captured moments ago. Closing the trace here
            # would throw away the aim data and the capture from the first call.
            look_debug.note_event(f"reused recent frame ({reason})")
            # Reading a frame (esp. text) can keep Gemini thinking silently
            # past the default watchdog — give THIS turn a longer window so
            # the answer isn't cut off seconds before it arrives.
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

        # Aim BEFORE capturing. `look` has no parameters, so without this the
        # frame is whatever the head happened to face — the model then answers
        # confidently about the wrong scene. Bounded by LOOK_AIM_DEADLINE_S and
        # never fatal: a failed aim still captures, because dead air in a live
        # turn is worse than an imperfectly framed frame.
        # Own the body for aim AND capture. An emotion animation landing between
        # them re-poses the head on every joint (recordings are absolute, roll
        # included), which is how a "curious" reaction ends up pointing the
        # camera at the ceiling for a visual question.
        from hal.drivers.tracking.aim import servo_ownership

        with servo_ownership():
            # None when aiming is disabled or raised — the settle below then
            # falls back to the base value rather than NameError-ing the look.
            res: Any = None
            if config.LOOK_AIM_ENABLED:
                try:
                    from hal.drivers.tracking.aim import aim_for_look, filler_ownership
                    from hal.telemetry import voice_metrics

                    # LIVE uses the tool's exact input key. Never charge a
                    # delayed filler to whichever interaction is newest now.
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
                    # Announce the capture only when the aim actually had work to
                    # do AND got somewhere — an already-centred shutter fires in
                    # a few hundred ms and needs no narration, and a failed one
                    # has already said `look_lost`.
                    #
                    # `iterations > 0` alone was not enough: a failed aim still
                    # counts its bearing steps, so "I can't find you" was
                    # followed by "let me take a look", which tells the story
                    # backwards. Audible once the look-around made the failure
                    # path seconds long instead of instant.
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

        # Legacy providers resolve the tool call BEFORE the frame goes out. Both the image and
        # the replayed audio below travel on `send_realtime_input`, which Gemini
        # refuses while a call it emitted is unanswered — so leaving this call
        # pending does not merely risk a 1008, it means the gate in
        # `_async_send_input` drops the frame AND every replayed audio frame,
        # and the turn ends with nothing to answer from (main-agent fallback).
        #
        # This path used to send no result at all. That worked only because the
        # gate carried a 10s expiry and a look with a slow aim (deadline 8s)
        # usually outlived it — flaky by construction. The expiry is gone, so
        # the call has to be answered rather than waited out.
        #
        # trigger_response=True is the only variant that reaches Gemini's
        # send_tool_response and clears the gate; trigger_response=False is the
        # fire-and-forget path, which deliberately tells Gemini nothing and
        # therefore leaves the session quarantined. The response it triggers is
        # cancelled by replay only on the legacy path. Status-aware async
        # sessions instead attach the frame to the tool result itself so it is
        # available to the current interaction, not a later audio turn.
        # Tell the model whether the aim actually found the user, not just that
        # a frame is coming. Without it the model cannot tell a well-framed shot
        # from "wherever the camera happened to be pointing after failing to
        # find anyone", so it answers both with the same confidence — the
        # "embarrassingly wrong" the tool description itself warns about.
        #
        # The frame is still sent. `look` is mostly asked about OBJECTS ("what
        # am I holding?", "read this label"), and the case where the person
        # detector fails is exactly the case where the object is filling the
        # frame — the asker close, clipped by the edge, occluded by the very
        # thing they are asking about. Withholding the image there would break
        # the question in the pose people actually hold things in.
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
        # Stamp the SEND, not the call entry. `now` is taken before the aim, so
        # a 3s aim made the guard below expire 3s early — the replay landed at
        # 12.6s against a 10s window and re-aimed from scratch instead of
        # reusing the frame (device trace 2026-08-19).
        self._last_look_sent_monotonic = time.monotonic()
        # The replayed turn (frame + re-committed audio) inherits the same
        # risk of a long silent think over the frame; extend its watchdog too.
        # receive() clears the override when the replayed turn ends.
        self._agent.extend_recv_timeout(config.REALTIME_LOOK_RECV_TIMEOUT_S)
        # Persist the SAME frame so that if this turn later delegates / falls
        # back to the main agent (e.g. Gemini times out mid-turn), the agent
        # reuses it instead of taking a fresh snapshot. See turn_dispatch.
        import hal.app_state as state

        with look_debug.stage("persist"):
            saved_path: str | None = self._persist_look_frame(frame)
        look_debug.note_capture(saved_path)
        # Stash a servable copy so the frame can appear in the turn the user
        # actually sees. It is attached to that turn's message as a
        # [snapshot: ...] marker by turn_dispatch — NOT posted as an event of
        # its own, which showed up as a bare path in a turn with no context.
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
        # Extended Thinking keeps the tool interaction alive after its filler.
        # Replaying user activity would interrupt that work. Legacy sessions
        # without authoritative interaction status retain their replay path.
        return not continue_interaction

    @staticmethod
    def _capture_frame(settle_s: float = 0.3) -> Any:
        """Return the latest camera frame (BGR ndarray) downscaled for cost, or
        None if no camera. Reads HAL camera state in-process (no HTTP loopback);
        mirrors the wait/disable handling of routes.camera.camera_snapshot.

        Uses capture_still, so servos (animation loop + tracker worker) are
        frozen and the frame is guaranteed captured after the arm went quiet —
        motion blur made Gemini misread scenes. Costs up to ~settle+timeout
        only while the servos are actually moving; when they are still (or the
        device has none — animation_service is None) it returns immediately."""
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
                    # 2.0s, not less: a camera woken from disabled (cap.start()
                    # above) can take over a second to deliver its first frame.
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
        """Save the look frame to disk and record it in app_state so a delegate /
        fallback turn can hand it to the main agent by path (see turn_dispatch).
        Best-effort: a write failure just means the agent snapshots fresh.
        Returns the saved path (also logged per capture for debugging: pull the
        file and compare it against the model's answer)."""
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
            # Drop the previous look frame so they don't pile up (the snapshot ring
            # only tracks /camera/snapshot saves, not these).
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
        """Send a text message to the agent as context (non-blocking).

        Used to feed back results from the main system after delegation,
        so the agent knows what happened.
        """
        if (
            config.REALTIME_PROVIDER.strip().lower() == "gemini"
            and gemini_needs_idle_workaround()
        ):
            # 2.5 native-audio via google-genai is sensitive to non-response
            # clientContent turns (`turn_complete=False`) mixed with audio turns:
            # even when sent before audio, repeated context/TTS-history injections
            # can leave the session in a state that later closes with WS 1011.
            # Keep its live session on the same wire shape as the browser probe:
            # audio realtimeInput + tool responses only. 3.1 is NOT sensitive →
            # allow text context (so per-turn [TURN CONTEXT] reaches the model).
            # INFO, not debug: this silently drops [TURN CONTEXT] (speaker identity,
            # language reminder) and the symptom downstream is "the model addresses
            # the wrong person" with nothing in the log to explain it.
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
        """Persist a user turn that the main agent will answer.

        The live provider retains the audio turn only while its current session
        survives. Record the handoff before OpenClaw replies so a Gemini session
        recycle cannot make the device forget what the user asked.
        """
        self.save_turn(
            user_text=user_text,
            agent_text="[This request was handed to the main agent; its spoken reply follows.]",
        )

    def save_main_agent_reply_fragment(self, text: str) -> None:
        """Persist a spoken main-agent reply for future realtime sessions.

        Main-agent TTS is sentence-streamed, so one logical reply may arrive as
        multiple fragments. Retaining each fragment preserves the complete
        answer without relying on the ephemeral ``[TTS HISTORY]`` injection.
        """
        if text.strip():
            self.save_turn(user_text="[Main agent reply]", agent_text=text)

    def send_function_result(self, call_id: str, output: str) -> None:
        """Send a function call result back to the model."""
        if self._agent is not None:
            self._agent.send([FunctionCallResultInput(call_id=call_id, output=output)])
