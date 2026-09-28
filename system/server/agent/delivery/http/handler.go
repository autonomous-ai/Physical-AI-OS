package http

import (
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/skillcontext/mood"
	"go.autonomous.ai/os/system/skillcontext/musicsuggestion"
	"go.autonomous.ai/os/system/skillcontext/posture"
	"go.autonomous.ai/os/system/skillcontext/wellbeing"
	"go.autonomous.ai/os/system/statusled"
)

// AgentHandler handles OpenClaw gateway WebSocket events and exposes monitor endpoints.
type AgentHandler struct {
	externalHistoryObserver func(runID string, failed bool)

	agentGateway domain.AgentGateway
	monitorBus   *monitor.Bus
	statusLED    *statusled.Service
	config       *config.Config // device type → capability gate for agent HW markers

	// lastLLMLimitTTS debounces the spoken LLM-usage-limit notice (unix ms).
	// The backend's limit banner streams as several sentences and recurs on
	// every turn while the plan is exhausted — only the first chunk within
	// the window speaks; the rest are dropped. See deliverTTS.
	lastLLMLimitTTS atomic.Int64

	// speechWatermarkMs is the unix-ms mark left by the physical cancel
	// gesture (single click → POST /api/agent/speech/cancel). Every reply
	// belonging to a turn that was CREATED at or before this mark is dropped
	// in deliverTTS instead of reaching the speaker — the turns themselves
	// keep running (tools still fire, web chat and history still receive the
	// text), they just lose the speaker. Turns created after the mark speak
	// normally, which is what makes "click, then say something new" work
	// while a backlog of older turns is still draining.
	//
	// A monotone watermark needs no clearing: a later click only moves it
	// forward, and every new turn is on the far side of it. That is why this
	// is a timestamp and not an is-suppressed flag — a flag cannot tell the
	// backlog apart from the sentence the user just asked for.
	speechWatermarkMs atomic.Int64

	// autoSpeechWatermarkMs is the same mark stamped by the SYSTEM instead of
	// the user: the realtime voice agent answered a NEWER utterance out loud
	// while the main agent was still working on the previous one. The main
	// agent runs one turn at a time (activeTurn is a single flag), so this is
	// one stale answer, not a backlog — but it is the answer to a question the
	// user has visibly moved on from, and speaking it a moment later in a
	// different voice is what this prevents.
	//
	// Deliberately a SECOND mark rather than a reuse of speechWatermarkMs. The
	// click is an explicit "stop", so it also drops the turn's servo/LED
	// markers; this one is a machine judgement and takes the speaker only. If
	// it dropped HW markers too, a turn whose body the user really did ask for
	// ("turn the light green") would silently never run because the user
	// happened to say something else while it worked.
	//
	// The line is speech-vs-hardware, NOT click-vs-auto: pending fillers are
	// dropped by both, because a filler is a promise that an answer is coming
	// rather than an action the user requested.
	autoSpeechWatermarkMs atomic.Int64

	// runFirstSeenMs records when a runID was first observed by deliverTTS,
	// for runIDs whose creation time cannot be read off the id itself.
	// Device-issued ids carry it ("device-chat-7-1755600000000"); channel ids
	// do not ("tg-<messageID>"). For those, first-speech time is the best
	// available proxy for turn age: a Telegram turn already talking when the
	// user clicked is backlog and gets muted, one whose first sentence lands
	// after the click is genuinely new and speaks. Pruned by age in
	// runCreatedAtMs so it cannot grow without bound.
	runFirstSeenMu sync.Mutex
	runFirstSeenMs map[string]int64

	// ttsTurnOrder assigns each agent turn a local, monotonic sequence when
	// its lifecycle starts. HAL receives this sequence with queued speech, so
	// late HTTP posts from an older turn cannot reclaim the speaker after a
	// newer turn has already produced a reply.
	ttsTurnMu      sync.Mutex
	ttsTurnOrder   map[string]uint64
	ttsTurnNextSeq uint64

	// assistantBuf accumulates assistant deltas per runId so we can send the
	// full text to TTS when the agent turn ends (lifecycle "end").
	//
	// streamedCleanLen tracks bytes of the HW-stripped reply already
	// dispatched to TTS by trySentenceFlush. Only the FIRST sentence is
	// streamed mid-turn — chaining each sentence as its own /voice/speak
	// POST produced a ~400ms TTFB gap between sentences (choppy). The
	// remainder goes through /voice/speak-queue at lifecycle:end, which
	// Python pre-synthesises while sentence 1 is still playing so the rest
	// of the reply chains on with no audible gap. Shares assistantMu.
	assistantMu      sync.Mutex
	assistantBuf     map[string]*strings.Builder
	streamedCleanLen map[string]int
	// ADDED 2026-05-26: count of leading HW markers fired at stream-time per
	// runID. Used at lifecycle:end to skip already-fired markers (avoid
	// double-fire). Cleared on lifecycle:end / channel-turn finalize. Shares
	// assistantMu — same per-runID scope as the buffer it tracks markers in.
	firedHWCount map[string]int

	// ttsSuppressReasons tracks runIDs that should skip TTS on lifecycle end.
	// Value is the reason: "music_playing" (speaker shared with audio) or
	// "already_spoken" (TTS tool intercepted and already routed to speaker).
	ttsSuppressMu      sync.Mutex
	ttsSuppressReasons map[string]string

	// harnessReplies holds agent runs whose final response is delivered directly
	// from the paired Harness agent rather than generated by the device runtime.
	harnessRepliesMu sync.Mutex
	harnessReplies   map[string]harnessReplyState
	// Last displayed preparation per active run, protected by harnessRepliesMu.
	harnessPreparationProgress map[string]string

	// runIDMap maps OpenClaw-assigned UUIDs back to device-originated idempotencyKeys.
	// When lifecycle_start arrives with UUID while a device trace is active, we store
	// the mapping so all subsequent events for that UUID use the device ID for flow tracing.
	// taskRunIDs is telemetry-only; never use it for playback or dispatch.
	taskRunIDsMu sync.Mutex
	taskRunIDs   map[string]string

	runIDMapMu sync.Mutex
	runIDMap   map[string]string // OpenClaw UUID → device idempotencyKey

	// lastEmotion tracks the most recent emotion expressed by the agent.
	lastEmotionMu sync.Mutex
	lastEmotion   string

	// toolArgsByCall carries a tool call's arguments from its "start" event to
	// its "end" event, keyed by toolCallId. See camera_snapshot.go.
	toolArgsMu     sync.Mutex
	toolArgsByCall map[string]string

	// channelRuns tracks runs confirmed from a real channel user (Telegram/etc.)
	// via senderLabel. Prevents TTS when a Telegram UUID gets mapped to a
	// sensing trace (race: flowRunID becomes device-sensing-* → isChannelRun false).
	channelRunsMu sync.Mutex
	channelRuns   map[string]bool

	// interleavedDMByRunID captures Telegram chat_ids when a Telegram message
	// is injected mid-turn into a device-issued run (queue mode). At lifecycle.end
	// the reply is routed back to that chat instead of TTS — fixes "the device
	// answered Telegram question on the speaker" when sensing/voice was the
	// run originator. Protected by channelRunsMu.
	interleavedDMByRunID map[string]string

	// cronFireRuns tracks runs initiated by an OpenClaw scheduled cron fire.
	// Populated when a lifecycle_start (UUID runId, no lamp- prefix) arrives
	// shortly after an event:"cron" (action:"started") — OpenClaw's cron
	// event omits sessionKey for sessionTarget="main" jobs, so we can't
	// correlate by session and instead consume from a FIFO timestamp queue.
	// Membership forces isChannelRun=false so the device speaker fires.
	cronFireRunsMu sync.Mutex
	cronFireRuns   map[string]bool

	// cronFireExpected is a FIFO queue of unix-ms timestamps from recent
	// cron "started" events. Each lifecycle_start with a UUID runId
	// consumes the oldest entry if it falls within cronFireWindowMs.
	// Stale entries (older than the window) are pruned on each access.
	cronFireExpectedMu sync.Mutex
	cronFireExpected   []int64

	// channelTurns tracks active channel-initiated turns (Telegram, etc.) keyed
	// by sessionKey. OpenClaw 5.x gates the `agent` lifecycle stream behind
	// isControlUiVisible, so non-device-originated runs receive only
	// `session.message` / `session.tool` / `sessions.changed`. chat_input,
	// lifecycle synthesis, and HW marker firing for those turns must be
	// driven from `session.message` here. Each entry holds the synthetic
	// device runId, accumulated assistant text, and turn metadata.
	channelTurnMu sync.Mutex
	channelTurns  map[string]*channelTurnState

	// agentLifecycleAt tracks when an `event=agent` lifecycle.start last fired
	// per sessionKey. Used by the session.message handler to skip turns that
	// are already being driven by the agent path (cron heartbeat fires both
	// streams; real user telegram fires only session.message).
	//
	// activeRunIDBySession tracks the in-flight runID per session so the
	// session.message handler can attribute interleaved channel messages to
	// the running turn even when the message itself is skipped.
	agentLifecycleMu     sync.Mutex
	agentLifecycleAt     map[string]int64
	activeRunIDBySession map[string]string

	// streamStats tracks per-run streaming counters and accumulated text for
	// JSONL emission of agent_first_token / agent_last_token (assistant
	// stream) and thinking_first_token / thinking_last_token (extended
	// thinking stream). Live deltas already flow through monitorBus but the
	// JSONL persist layer drops them — these summary events are the
	// persisted projection that Flow Monitor renders from on reload.
	streamStatsMu sync.Mutex
	streamStats   map[string]*runStreamStats

	// errorRecoveredRuns tracks runs whose reply was salvaged by
	// tryRecoverIncompleteTurn (see handler_error_recovery.go) so chat-stream
	// error banners — including the gateway's ~15s-later retry error — are
	// suppressed instead of overwriting the recovered reply. TTL-pruned.
	errorRecoveredMu   sync.Mutex
	errorRecoveredRuns map[string]time.Time

	// compacting prevents duplicate /compact sends while one is in progress.
	compacting atomic.Bool

	// newSessioning prevents duplicate sessions.new sends while one is
	// in flight. Cooldown is shorter than compacting because new-session
	// completes server-side instantly.
	newSessioning atomic.Bool

	// turnsSinceRotation counts agent turns since the last auto-new-session.
	// Feeds the sessionRotator decision for backends (e.g. Hermes) whose
	// reported token count understates real session size. Reset on rotation.
	turnsSinceRotation atomic.Int64
}

// runStreamStats is the per-run streaming bookkeeping that backs the
// agent_*_token / thinking_*_token JSONL events. Independent of assistantBuf
// (which serves TTS flush) so the two paths can't interfere.
type runStreamStats struct {
	assistantFirstSeen bool
	assistantFirstAt   time.Time
	assistantChunks    int
	assistantChars     int
	assistantText      strings.Builder

	thinkingFirstSeen bool
	thinkingChunks    int
	thinkingChars     int
	thinkingText      strings.Builder
}

// channelTurnState tracks the in-flight assistant response for a channel
// session (Telegram/etc.) so HW markers in the final assistant message can
// be extracted and fired even when no `agent` lifecycle event arrives.
type channelTurnState struct {
	runID       string
	senderLabel string
	telegramID  string
	accumulated strings.Builder
	startedAtMs int64
}

// cronFireWindowMs is the max delay between an OpenClaw cron "started" event
// and the lifecycle_start it precedes. Observed ~2s in practice; 10s leaves
// generous headroom for slow/loaded runs without false-positive correlations.
const cronFireWindowMs int64 = 10_000

// ProvideAgentHandler returns an OpenClaw events handler.
func ProvideAgentHandler(gw domain.AgentGateway, bus *monitor.Bus, sled *statusled.Service, cfg *config.Config) *AgentHandler {
	// Init flow emitter here so ws_connect events (fired from StartWS before any HTTP request)
	// are broadcast to SSE. The device is single-user so the global trace ID is sufficient;
	// concurrent turn interleaving is not a concern in normal operation.
	flow.Init(bus, config.OSVersion)
	mood.Init()
	wellbeing.Init()
	musicsuggestion.Init()
	posture.Init()
	// Populate OpenClaw version cache in the background so the first Status
	// poll has it ready.
	go populateOpenClawVersion()
	go populateHermesVersion()
	go populatePicoclawVersion()
	go populateCodexVersion()
	go populateClaudeCodeVersion()
	go populateOpenCodeVersion()
	return &AgentHandler{
		agentGateway:         gw,
		monitorBus:           bus,
		statusLED:            sled,
		config:               cfg,
		assistantBuf:         make(map[string]*strings.Builder),
		streamedCleanLen:     make(map[string]int),
		firedHWCount:         make(map[string]int),
		streamStats:          make(map[string]*runStreamStats),
		ttsSuppressReasons:   make(map[string]string),
		harnessReplies:       make(map[string]harnessReplyState),
		runIDMap:             make(map[string]string),
		channelRuns:          make(map[string]bool),
		interleavedDMByRunID: make(map[string]string),
		cronFireRuns:         make(map[string]bool),
		channelTurns:         make(map[string]*channelTurnState),
		agentLifecycleAt:     make(map[string]int64),
		activeRunIDBySession: make(map[string]string),
		errorRecoveredRuns:   make(map[string]time.Time),
		runFirstSeenMs:       make(map[string]int64),
		ttsTurnOrder:         make(map[string]uint64),
	}
}

// IsSleeping reports whether the device is asleep, for SensingHandler's gate on
// passive sensing events. HAL decides: lastEmotion only moves when the AGENT
// expresses an emotion, so a wake that skips the agent (button tap, web UI,
// direct POST to HAL) left this stuck on "sleepy" and dropped sensing on an
// awake device. Conversely, os-server restart clears lastEmotion while HAL
// can remain asleep. Only use lastEmotion as a fallback when HAL is unreachable.
func (h *AgentHandler) IsSleeping() bool {
	return h.isSleeping(hal.GetSleeping)
}

func (h *AgentHandler) isSleeping(getSleeping func() (bool, error)) bool {
	h.lastEmotionMu.Lock()
	believesAsleep := h.lastEmotion == "sleepy"
	h.lastEmotionMu.Unlock()
	// Devices without `expression` never mount HAL's /emotion route, so asking
	// would 404 on every event (same gate as fetchHALEmotion).
	if !device.Has(h.config.DeviceTypeOrDefault(), device.CapExpression) {
		return believesAsleep
	}
	sleeping, err := getSleeping()
	if err != nil {
		slog.Debug("sleep gate: HAL unreachable, keeping lastEmotion",
			"component", "agent", "error", err)
		return believesAsleep
	}
	return sleeping
}

// consumeInterleavedDM atomically reads and removes the captured Telegram
// chat_id for runID. Empty result means no interleaved Telegram message was
// recorded for this turn — the normal TTS path applies.
func (h *AgentHandler) consumeInterleavedDM(runID string) string {
	if runID == "" {
		return ""
	}
	h.channelRunsMu.Lock()
	defer h.channelRunsMu.Unlock()
	cid := h.interleavedDMByRunID[runID]
	if cid != "" {
		delete(h.interleavedDMByRunID, runID)
	}
	return cid
}
