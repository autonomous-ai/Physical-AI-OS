// Package picoclaw implements domain.AgentGateway against a PicoClaw runtime
// reached over a persistent WebSocket.
package picoclaw

import (
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

// Compile-time check: *PicoclawService implements domain.AgentGateway.
var _ domain.AgentGateway = (*PicoclawService)(nil)

// reSnapshotPath / rePoseBucketMarker / rePoseWorstMarker mirror the openclaw
// regexes so the drain pipeline strips the same markers before send.
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

// PicoclawService is the PicoClaw backend implementation of domain.AgentGateway.
type PicoclawService struct {
	config     *config.Config
	monitorBus *monitor.Bus
	statusLED  *statusled.Service

	// Persistent WebSocket.
	wsMu            sync.Mutex
	wsConn          *websocket.Conn
	wsConnected     atomic.Bool
	wsConnectedAt   atomic.Int64     // unix seconds when the socket last became ready
	wsHasConnected  atomic.Bool      // skip "reconnect" TTS on first successful connect
	reconnectNotice reconnect.Notice // announce a reconnect only after a real outage

	// Turn lifecycle.
	sendMu       sync.Mutex // Serializes admission: this protocol has no response request IDs.
	activeTurn   atomic.Bool
	busySince    atomic.Int64
	pendingRunID atomic.Value // string
	currentRunID atomic.Value // string
	reqCounter   atomic.Int64

	// Session state.
	sessionUUID atomic.Value // string

	// lastCompressAt is PicoClaw's most recent compress_at_tokens
	lastCompressAt atomic.Int64

	// Pending sensing events buffered while busy.
	pendingEventsDrainMu sync.Mutex
	pendingEventsMu      sync.Mutex
	pendingEvents        []pendingEvent

	// Run trackers (guard / broadcast / web_chat / silent / pose bucket).
	guardRunsMu sync.Mutex
	guardRuns   map[string]string

	broadcastRunsMu sync.Mutex
	broadcastRuns   map[string]bool

	webChatRunsMu sync.Mutex
	webChatRuns   map[string]bool

	silentRunsMu sync.Mutex
	silentRuns   map[string]bool

	poseBucketRunsMu sync.Mutex
	poseBucketRuns   map[string]poseBucketInfo

	// Channel senders (Telegram).
	channels []domain.ChannelSender

	// Pending chat traces (idempotencyKey ↔ message text for MatchPendingByMessage).
	pendingChatMu  sync.Mutex
	pendingChatBuf []pendingTrace

	// Recent outbound texts (echo-suppression for session.message handler).
	recentOutboundMu    sync.Mutex
	recentOutboundTexts []recentOutbound

	// Serializes read-modify-write of config.json (MCP entry writes).
	mcpMu sync.Mutex

	// ackHookEnabled mirrors OpenClaw's emotion-acknowledge hook
	ackHookEnabled bool
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

// ProvideService constructs the PicoClaw service.
func ProvideService(cfg *config.Config, bus *monitor.Bus, sled *statusled.Service) *PicoclawService {
	s := &PicoclawService{
		config:         cfg,
		monitorBus:     bus,
		statusLED:      sled,
		guardRuns:      make(map[string]string),
		broadcastRuns:  make(map[string]bool),
		webChatRuns:    make(map[string]bool),
		silentRuns:     make(map[string]bool),
		poseBucketRuns: make(map[string]poseBucketInfo),
	}
	s.channels = []domain.ChannelSender{
		&TelegramSender{svc: s},
	}
	s.ackHookEnabled = ackEmotionEnabled(cfg.DeviceTypeOrDefault())
	return s
}

// Name returns the display name surfaced via /api/openclaw/status.
func (s *PicoclawService) Name() string { return "PicoClaw" }

// IsReady reports whether the persistent WebSocket is currently connected.
func (s *PicoclawService) IsReady() bool { return s.wsConnected.Load() }

// ConnectedAt returns the unix-seconds timestamp when the socket last connected.
func (s *PicoclawService) ConnectedAt() int64 { return s.wsConnectedAt.Load() }

// AgentUptime — PicoClaw does not report process uptime over the wire, so we
// have no value independent of the local WS reconnect cycle.
func (s *PicoclawService) AgentUptime() int64 { return 0 }

// markOutboundChat / IsRecentOutboundChat mirror openclaw.PicoclawService.
func (s *PicoclawService) markOutboundChat(text string) {
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
func (s *PicoclawService) IsRecentOutboundChat(text string) bool {
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
