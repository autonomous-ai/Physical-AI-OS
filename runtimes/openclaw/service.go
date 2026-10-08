package openclaw

import (
	"encoding/json"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/reconnect"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/statusled"
)

const (
	defaultGatewayWSURL = "ws://127.0.0.1:18789"
	customProviderName  = "autonomous"
	// autonomousProviderAPI is the fallback wire protocol when the models API omits `api`.
	autonomousProviderAPI = "anthropic-messages"
	defaultGatewayMode    = "local"
	defaultGatewayBind    = "loopback"
	defaultGatewayPort    = 18789
	openclawRuntimeUser   = "root"
)

// Compile-time check: *OpenclawService implements domain.AgentGateway.
var _ domain.AgentGateway = (*OpenclawService)(nil)

// reSnapshotPath matches [snapshot: /path/to/file.jpg] markers in sensing messages.
var reSnapshotPath = regexp.MustCompile(`\[snapshot:\s*[^\]]+\]`)

// Pose bucket markers — emitted by hal motion.py on motion.activity when a posture nudge folds in.
var rePoseBucketMarker = regexp.MustCompile(`\[pose_bucket:\s*([^\]]+)\]\n?`)
var rePoseWorstMarker = regexp.MustCompile(`\[pose_worst:\s*([^\]]+)\]\n?`)

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

// OpenclawService provides setup, reset, restart of openclaw config/gateway and StartWS.
type OpenclawService struct {
	config        *config.Config
	monitorBus    *monitor.Bus
	statusLED     *statusled.Service
	wsConnected   atomic.Bool  // true when gateway WebSocket is connected and ready to receive messages
	wsConnectedAt atomic.Int64 // unix seconds when wsConnected last flipped to true; 0 when disconnected
	// agentStartedAt is the unix-seconds timestamp the OpenClaw gateway process started, derived from the server.uptimeMs field of the hello-ok response at handshake.
	agentStartedAt  atomic.Int64
	activeTurn      atomic.Bool      // true while agent is processing a turn (lifecycle start → end)
	busySince       atomic.Int64     // unix milli when activeTurn was last set to true; used to expire stuck busy state
	wsHasConnected  atomic.Bool      // true after first successful WS connect (skip reconnect TTS on boot)
	reconnectNotice reconnect.Notice // announce a reconnect only after a real outage

	// wsConn is the active WebSocket connection; guarded by wsMu.
	wsConn *websocket.Conn
	wsMu   sync.Mutex
	// lastSessionKey is the most recent session key observed from agent lifecycle events.
	lastSessionKey atomic.Value // string
	// reqCounter is used to generate unique request IDs for outgoing RPC calls.
	reqCounter atomic.Int64

	// pendingRPC tracks in-flight RPC requests waiting for a response.
	pendingRPCMu sync.Mutex
	pendingRPC   map[string]chan json.RawMessage // reqID → response channel

	// pendingEvents buffers sensing events received while agent is busy.
	pendingEventsDrainMu sync.Mutex // serialize offline requeue and reconnect drains
	pendingEventsMu      sync.Mutex
	pendingEvents        []pendingEvent

	// guardRuns tracks runIDs that are guard-active sensing turns.
	guardRunsMu sync.Mutex
	guardRuns   map[string]string // runID → snapshot path

	// channels is the list of registered messaging channel senders (Telegram, Discord, Slack, etc.).
	channels []domain.ChannelSender

	// broadcastRuns tracks runIDs whose agent response should be broadcast to all messaging channels alongside TTS (e.g. music.mood confirmations).
	broadcastRunsMu sync.Mutex
	broadcastRuns   map[string]bool

	// webChatRuns tracks runIDs originating from the web monitor chat.
	webChatRunsMu sync.Mutex
	webChatRuns   map[string]bool

	// silentRuns tracks runIDs whose spoken reply must be suppressed even though the agent still processes the turn.
	silentRunsMu sync.Mutex
	silentRuns   map[string]bool

	// poseBucketRuns associates a motion.activity runID with the hal pose bucket whose window just fired.
	poseBucketRunsMu sync.Mutex
	poseBucketRuns   map[string]poseBucketInfo

	// primarySyncMu serialises syncPrimaryFromFile against concurrent debounce firings and UpdatePrimaryModel.
	primarySyncMu sync.Mutex

	// pendingChat tracks outbound chat.sends not yet paired with a lifecycle.
	pendingChatMu  sync.Mutex
	pendingChatBuf []pendingTrace
	// pendingTaskBuf is telemetry-only evidence, guarded by pendingChatMu.
	pendingTaskBuf []pendingTrace

	// recentOutboundTexts is a small ring buffer of message texts the os server sent via chat.send (wake greeting, ambient guard, sensing events).
	recentOutboundMu    sync.Mutex
	recentOutboundTexts []recentOutbound
}

type recentOutbound struct {
	text string
	ts   int64 // unix ms
}

const recentOutboundWindowMs int64 = 30_000
const recentOutboundMaxEntries = 32

// pendingTrace pairs a chat.send idempotencyKey with the message text and send time.
type pendingTrace struct {
	runID   string
	message string
	sentAt  time.Time
}

// poseBucketInfo carries the hal bucket identifier and the pre-selected worst-snapshot filenames for a single motion.activity turn.
type poseBucketInfo struct {
	bucketID  string
	filenames []string
	markedAt  time.Time
}

// ProvideService constructs the openclaw service.
func ProvideService(cfg *config.Config, bus *monitor.Bus, sled *statusled.Service) *OpenclawService {
	s := &OpenclawService{
		config:         cfg,
		monitorBus:     bus,
		statusLED:      sled,
		pendingRPC:     make(map[string]chan json.RawMessage),
		guardRuns:      make(map[string]string),
		broadcastRuns:  make(map[string]bool),
		webChatRuns:    make(map[string]bool),
		silentRuns:     make(map[string]bool),
		poseBucketRuns: make(map[string]poseBucketInfo),
	}
	s.channels = []domain.ChannelSender{
		&TelegramSender{svc: s},
	}
	return s
}

// Name returns the display name of this agent gateway.
func (s *OpenclawService) Name() string {
	return "OpenClaw"
}

// Version returns the cached OpenClaw binary version (e.g. "2026.5.27"), or empty when undetected.
func (s *OpenclawService) Version() string {
	return GetOpenClawVersion()
}

// markOutboundChat records an os-server-sent chat.send message text so the SSE session.message handler can skip its echo.
func (s *OpenclawService) markOutboundChat(text string) {
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

// IsRecentOutboundChat reports whether the os server sent this text recently.
func (s *OpenclawService) IsRecentOutboundChat(text string) bool {
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

// IsReady returns true when the gateway WebSocket is connected and OpenClaw is ready to receive messages.
func (s *OpenclawService) IsReady() bool {
	return s.wsConnected.Load()
}

// ConnectedAt returns the unix-seconds timestamp when the WS connection last became ready, or 0 when not currently connected.
func (s *OpenclawService) ConnectedAt() int64 {
	return s.wsConnectedAt.Load()
}

// AgentUptime returns the OpenClaw gateway process uptime in seconds, derived from server.uptimeMs in the hello-ok response.
func (s *OpenclawService) AgentUptime() int64 {
	if !s.wsConnected.Load() {
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
