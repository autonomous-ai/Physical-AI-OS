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

// AgentHandler handles agent gateway WebSocket events and exposes monitor endpoints.
type AgentHandler struct {
	externalHistoryObserver func(runID string, failed bool)

	agentGateway domain.AgentGateway
	monitorBus   *monitor.Bus
	statusLED    *statusled.Service
	config       *config.Config // device type → capability gate for agent HW markers

	// lastLLMLimitTTS debounces the spoken LLM-usage-limit notice (unix ms).
	lastLLMLimitTTS atomic.Int64

	// speechWatermarkMs: unix-ms mark from the user cancel gesture. Speech of turns
	// created at or before it is dropped (turns keep running). Monotone — never cleared.
	speechWatermarkMs atomic.Int64

	// autoSpeechWatermarkMs: system-set mark when realtime answered a newer utterance.
	// Drops speech and fillers only, never HW markers (unlike speechWatermarkMs).
	autoSpeechWatermarkMs atomic.Int64

	// runFirstSeenMs: first-speech time for runIDs without an embedded timestamp
	// (e.g. "tg-<id>"); proxy for turn age. Pruned in runCreatedAtMs.
	runFirstSeenMu sync.Mutex
	runFirstSeenMs map[string]int64

	// ttsTurnOrder: monotonic per-turn sequence sent to HAL so late posts from an
	// older turn cannot reclaim the speaker from a newer one.
	ttsTurnMu      sync.Mutex
	ttsTurnOrder   map[string]uint64
	ttsTurnNextSeq uint64

	// assistantBuf accumulates assistant deltas per runID; streamedCleanLen tracks
	// bytes already streamed to TTS; firedHWCount counts HW markers fired mid-stream
	// (skipped at lifecycle end to avoid double-fire). All guarded by assistantMu.
	assistantMu      sync.Mutex
	assistantBuf     map[string]*strings.Builder
	streamedCleanLen map[string]int
	firedHWCount     map[string]int

	// ttsSuppressReasons: runID → reason to skip TTS at lifecycle end
	// ("music_playing" or "already_spoken").
	ttsSuppressMu      sync.Mutex
	ttsSuppressReasons map[string]string

	// harnessReplies holds runs whose final reply comes from the paired Harness agent.
	harnessRepliesMu sync.Mutex
	harnessReplies   map[string]harnessReplyState
	// Last displayed preparation per active run, protected by harnessRepliesMu.
	harnessPreparationProgress map[string]string

	// taskRunIDs is telemetry-only; never use it for playback or dispatch.
	taskRunIDsMu sync.Mutex
	taskRunIDs   map[string]string

	runIDMapMu sync.Mutex
	runIDMap   map[string]string // gateway UUID → device idempotencyKey

	lastEmotionMu sync.Mutex
	lastEmotion   string

	// toolArgsByCall carries tool-call args from "start" to "end", keyed by toolCallId.
	toolArgsMu     sync.Mutex
	toolArgsByCall map[string]string

	// channelRuns marks runs confirmed from a real channel user; prevents TTS when
	// a channel UUID gets mapped to a sensing trace.
	channelRunsMu sync.Mutex
	channelRuns   map[string]bool

	// interleavedDMByRunID: Telegram chat_id injected mid-turn into a device run;
	// the reply is routed back to that chat instead of TTS. Guarded by channelRunsMu.
	interleavedDMByRunID map[string]string

	// cronFireRuns: runs started by a gateway cron fire (forces speaker output).
	cronFireRunsMu sync.Mutex
	cronFireRuns   map[string]bool

	// cronFireExpected: FIFO of recent cron "started" timestamps (unix ms), consumed
	// by the next UUID lifecycle_start within cronFireWindowMs.
	cronFireExpectedMu sync.Mutex
	cronFireExpected   []int64

	// channelTurns: active channel-initiated turns keyed by sessionKey, driven from
	// session.message because the gateway does not stream agent lifecycle for them.
	channelTurnMu sync.Mutex
	channelTurns  map[string]*channelTurnState

	// agentLifecycleAt: last agent lifecycle.start per sessionKey (dedupes
	// session.message turns); activeRunIDBySession: in-flight runID per session.
	agentLifecycleMu     sync.Mutex
	agentLifecycleAt     map[string]int64
	activeRunIDBySession map[string]string

	// streamStats: per-run counters backing the persisted *_first_token/*_last_token events.
	streamStatsMu sync.Mutex
	streamStats   map[string]*runStreamStats

	// errorRecoveredRuns: runs salvaged by tryRecoverIncompleteTurn; later error
	// banners are suppressed. TTL-pruned.
	errorRecoveredMu   sync.Mutex
	errorRecoveredRuns map[string]time.Time

	// compacting prevents duplicate /compact sends while one is in progress.
	compacting atomic.Bool

	// newSessioning prevents duplicate sessions.new sends while one is in flight.
	newSessioning atomic.Bool

	// turnsSinceRotation counts turns since the last auto-new-session. Reset on rotation.
	turnsSinceRotation atomic.Int64
}

// runStreamStats is per-run streaming bookkeeping for the *_token JSONL events.
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

// channelTurnState tracks the in-flight assistant response for a channel session.
type channelTurnState struct {
	runID       string
	senderLabel string
	telegramID  string
	accumulated strings.Builder
	startedAtMs int64
}

// cronFireWindowMs is the max delay between a cron "started" event and its
// lifecycle_start (observed ~2s).
const cronFireWindowMs int64 = 10_000

// ProvideAgentHandler returns an agent events handler.
func ProvideAgentHandler(gw domain.AgentGateway, bus *monitor.Bus, sled *statusled.Service, cfg *config.Config) *AgentHandler {
	// Init before StartWS so ws_connect events reach SSE.
	flow.Init(bus, config.OSVersion)
	mood.Init()
	wellbeing.Init()
	musicsuggestion.Init()
	posture.Init()
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

// IsSleeping reports whether the device is asleep. HAL is authoritative;
// lastEmotion is only a fallback when HAL is unreachable.
func (h *AgentHandler) IsSleeping() bool {
	return h.isSleeping(hal.GetSleeping)
}

func (h *AgentHandler) isSleeping(getSleeping func() (bool, error)) bool {
	h.lastEmotionMu.Lock()
	believesAsleep := h.lastEmotion == "sleepy"
	h.lastEmotionMu.Unlock()
	// Devices without `expression` have no HAL /emotion route.
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

// consumeInterleavedDM atomically reads and removes the captured Telegram chat_id
// for runID; "" means none.
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
