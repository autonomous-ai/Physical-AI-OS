// Package hermes implements domain.AgentGateway against the Hermes HTTP+SSE
// API server (OpenAI Responses API style). See docs/agentic/hermes.md for the full
// design — protocol mapping, session strategy, and the runtime boundaries with
// OpenClaw.
//
// Hermes is assumed to be running locally on the Pi at Hermes.BaseURL with
// all skills already provisioned. Device only acts as a per-request client and
// translates SSE events into the same domain.WSEvent shape that the OpenClaw
// handler at server/agent/delivery/http/handler_events.go consumes — so the
// downstream pipeline (HAL TTS, [HW:/...] markers, monitor SSE, sensing
// drain, Telegram fan-out) stays untouched.
package hermes

import (
	"context"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/statusled"
)

// Compile-time check: *HermesService implements domain.AgentGateway.
var _ domain.AgentGateway = (*HermesService)(nil)

// reSnapshotPath / rePoseBucketMarker / rePoseWorstMarker mirror the openclaw
// regexes so the drain pipeline strips the same markers before send. Kept as
// package vars (compile once) since drainPendingEvents fires per pending turn.
var (
	reSnapshotPath     = regexp.MustCompile(`\[snapshot:\s*[^\]]+\]`)
	rePoseBucketMarker = regexp.MustCompile(`\[pose_bucket:\s*([^\]]+)\]\n?`)
	rePoseWorstMarker  = regexp.MustCompile(`\[pose_worst:\s*([^\]]+)\]\n?`)
)

// extractPoseBucketMarkers pulls (bucket_id, filenames) from a sensing message.
func extractPoseBucketMarkers(message string) (string, []string) {
	bm := rePoseBucketMarker.FindStringSubmatch(message)
	if bm == nil {
		return "", nil
	}
	bucketID := strings.TrimSpace(bm[1])
	if bucketID == "" {
		return "", nil
	}
	wm := rePoseWorstMarker.FindStringSubmatch(message)
	var worst []string
	if wm != nil {
		for _, part := range strings.Split(wm[1], ",") {
			part = strings.TrimSpace(part)
			if part != "" {
				worst = append(worst, part)
			}
		}
	}
	return bucketID, worst
}

// HermesService is the Hermes backend implementation of domain.AgentGateway.
//
// Unlike openclaw.Service which holds a persistent WebSocket, Hermes is
// per-request: each SendChatMessage opens a POST /v1/responses with stream:true
// and reads SSE until response.completed. Lifecycle/busy state, run tracking,
// channel senders, and TTS plumbing are otherwise identical to openclaw.
type HermesService struct {
	nativeRunSteering atomic.Bool
	steeringMu        sync.Mutex
	steeringQueue     []managedChat
	steeringWake      chan struct{}
	runExpiries       chan runExpiry
	runtimeCtx        context.Context
	managedSession    string
	managedSessionSet bool

	config     *config.Config
	monitorBus *monitor.Bus
	statusLED  *statusled.Service
	httpClient *http.Client

	// Connection-state shadow. Hermes has no persistent socket, so these are
	// driven by the /health poller goroutine (see health.go).
	ready          atomic.Bool
	connectedAt    atomic.Int64 // unix seconds when ready last flipped true
	agentStartedAt atomic.Int64 // derived from /health/detailed.uptime_s if available
	hasConnected   atomic.Bool  // skip "reconnect" TTS on first successful poll

	// Turn lifecycle, mirrors openclaw.Service. activeTurn flips true on
	// SendChat (write) and false on response.completed (read).
	activeTurn atomic.Bool
	// Each HTTP stream owns its lifecycle; another stream ending must not clear it.
	inFlightStreams atomic.Int64
	drainMu         sync.Mutex
	busySince       atomic.Int64

	// Session/conversation state. sessionUUID is the X-Hermes-Session-Id header
	// captured from any response; conversation is the named channel everything
	// flows into (default "device-main").
	sessionUUID    atomic.Value // string
	lastResponseID atomic.Value // string — last response.id observed
	reqCounter     atomic.Int64

	// Conversation rotation (rotation.go). The gateway chains the whole history
	// into one response blob per turn keyed on conversation name, so a permanent
	// name accumulates a multi-MB / multi-million-token chain it must reconstruct
	// + recompress every turn. conversation holds the active name (boot-fresh +
	// per-rotation suffix); convOnce seeds bootStamp once; rotateSeq increments
	// per rotation.
	conversation atomic.Value // string
	convOnce     sync.Once
	bootStamp    int64
	rotateSeq    atomic.Int64

	// Handler registered via StartWS — kept here so the per-request SSE
	// consumer can dispatch translated domain.WSEvent frames into the same
	// pipeline as openclaw.
	handlerMu sync.Mutex
	handler   domain.AgentEventHandler

	// Pending sensing events buffered while busy.
	pendingEventsMu sync.Mutex
	pendingEvents   []pendingEvent

	// Run trackers (guard / broadcast / web_chat / pose bucket). All in-memory.
	guardRunsMu sync.Mutex
	guardRuns   map[string]string

	broadcastRunsMu sync.Mutex
	broadcastRuns   map[string]bool

	webChatRunsMu sync.Mutex
	webChatRuns   map[string]bool

	// silentRuns tracks runIDs whose spoken reply must be suppressed even though
	// the agent still processes the turn (e.g. voice_agent_handled). Mirrors the
	// openclaw backend.
	silentRunsMu sync.Mutex
	silentRuns   map[string]bool

	poseBucketRunsMu sync.Mutex
	poseBucketRuns   map[string]poseBucketInfo

	// Channel senders (Telegram).
	channels []domain.ChannelSender

	// ackHookEnabled mirrors OpenClaw's emotion-acknowledge hook: when the device
	// declares the `expression` capability, every visible turn flashes a "thinking"
	// face before the reply lands. Resolved once at construction from the shared
	// hook registry (skills.SupportedHooks). See emotion_ack.go.
	ackHookEnabled bool

	// Pending chat traces (mapping idempotencyKey ↔ message text for
	// MatchPendingByMessage). Hermes-side this is less critical since we own
	// the response.id immediately, but the SSE handler still calls these on
	// some paths so we keep parity.
	pendingChatMu  sync.Mutex
	pendingChatBuf []pendingTrace

	// Recent outbound texts (echo-suppression for session.message handler).
	recentOutboundMu    sync.Mutex
	recentOutboundTexts []recentOutbound

	// slackRunOrigin maps a runID → the Slack channel/thread an inbound HTTP-mode
	// Slack event came from, so the SSE handler can post the reply back (and suppress
	// TTS). Populated by HandleInboundSlack; consumed once by the agent event handler.
	// See runtimes/hermes/slack.go.
	slackRunOriginMu sync.Mutex
	slackRunOrigin   map[string]slackOrigin

	// slackStreams maps a runID → its live Slack streaming message (chat.startStream)
	// so the reply renders progressively under the native typing indicator. See
	// runtimes/hermes/slack_stream.go.
	slackStreamsMu sync.Mutex
	slackStreams   map[string]*slackStream

	// mcpMu serializes config.yaml read-modify-write in WriteMCPEntry/RemoveMCPEntry
	// (mcp.go) so concurrent connector.set writes cannot interleave.
	mcpMu sync.Mutex
}

type recentOutbound struct {
	text string
	ts   int64
}

const recentOutboundWindowMs int64 = 30_000
const recentOutboundMaxEntries = 32

type pendingTrace struct {
	runID   string
	message string
	sentAt  time.Time
}

type poseBucketInfo struct {
	bucketID  string
	filenames []string
	markedAt  time.Time
}

// ProvideService constructs the Hermes service. Wired via system/agent/factory.go
// when config.AgentRuntime == "hermes".
func ProvideService(cfg *config.Config, bus *monitor.Bus, sled *statusled.Service) *HermesService {
	s := &HermesService{
		config:         cfg,
		monitorBus:     bus,
		statusLED:      sled,
		httpClient:     &http.Client{Timeout: 0}, // per-request stream — no global timeout, use ctx
		guardRuns:      make(map[string]string),
		broadcastRuns:  make(map[string]bool),
		webChatRuns:    make(map[string]bool),
		silentRuns:     make(map[string]bool),
		poseBucketRuns: make(map[string]poseBucketInfo),
		slackRunOrigin: make(map[string]slackOrigin),
		slackStreams:   make(map[string]*slackStream),
	}
	s.channels = []domain.ChannelSender{
		&TelegramSender{svc: s},
		&SlackSender{svc: s},
	}
	s.ackHookEnabled = ackEmotionEnabled(cfg.DeviceTypeOrDefault())
	return s
}

// Name returns the display name surfaced via /api/openclaw/status.
func (s *HermesService) Name() string { return "Hermes" }

// IsReady reports whether the Hermes server has been reachable on a recent
// /health poll. Driven by the health poller goroutine, not by per-request SSE
// (a single failed POST does not flip readiness).
func (s *HermesService) IsReady() bool { return s.ready.Load() }

// ConnectedAt returns the unix-seconds timestamp when readiness last became
// true. Mirrors openclaw.HermesService.ConnectedAt for the monitor UI.
func (s *HermesService) ConnectedAt() int64 { return s.connectedAt.Load() }

// AgentUptime returns Hermes process uptime in seconds when /health/detailed
// has reported it. Returns 0 when the value has not yet been observed or the
// server is currently unreachable.
func (s *HermesService) AgentUptime() int64 {
	if !s.ready.Load() {
		return 0
	}
	startedAt := s.agentStartedAt.Load()
	if startedAt <= 0 {
		return 0
	}
	uptime := time.Now().Unix() - startedAt
	if uptime < 0 {
		return 0
	}
	return uptime
}

// markOutboundChat / IsRecentOutboundChat mirror openclaw.Service. Used by the
// session.message handler to skip echoes of Device-injected user messages
// (wake greeting, sensing events) that the server rebroadcasts.
func (s *HermesService) markOutboundChat(text string) {
	if text == "" {
		return
	}
	now := time.Now().UnixMilli()
	s.recentOutboundMu.Lock()
	defer s.recentOutboundMu.Unlock()
	cutoff := now - recentOutboundWindowMs
	pruned := s.recentOutboundTexts[:0]
	for _, r := range s.recentOutboundTexts {
		if r.ts >= cutoff {
			pruned = append(pruned, r)
		}
	}
	pruned = append(pruned, recentOutbound{text: text, ts: now})
	if len(pruned) > recentOutboundMaxEntries {
		pruned = pruned[len(pruned)-recentOutboundMaxEntries:]
	}
	s.recentOutboundTexts = pruned
}

// IsRecentOutboundChat reports whether Device sent this text recently.
func (s *HermesService) IsRecentOutboundChat(text string) bool {
	if text == "" {
		return false
	}
	now := time.Now().UnixMilli()
	cutoff := now - recentOutboundWindowMs
	s.recentOutboundMu.Lock()
	defer s.recentOutboundMu.Unlock()
	for _, r := range s.recentOutboundTexts {
		if r.ts >= cutoff && r.text == text {
			return true
		}
	}
	return false
}
