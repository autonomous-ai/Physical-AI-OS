// Package opencode implements domain.AgentGateway against the opencode CLI
// reached over a persistent WebSocket to a thin local bridge.
package opencode

import (
	"context"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/bwmarrin/discordgo"
	"github.com/gorilla/websocket"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/reconnect"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/statusled"
)

// Compile-time check: *OpenCodeService implements domain.AgentGateway.
var _ domain.AgentGateway = (*OpenCodeService)(nil)

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

// OpenCodeService is the OpenCode backend implementation of domain.AgentGateway.
type OpenCodeService struct {
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
	activeTurn           atomic.Bool
	busySince            atomic.Int64
	pendingMu            sync.Mutex
	pendingRuns          []pendingRun
	completedRequests    []pendingRun
	currentRequestID     atomic.Value // string
	sendChatMu           sync.Mutex
	pendingEventsDrainMu sync.Mutex
	currentRunID         atomic.Value // string
	reqCounter           atomic.Int64

	// Session state.
	sessionUUID atomic.Value // string

	// turnMu guards the per-turn accumulation state below.
	turnMu sync.Mutex
	// assistantParts collects the `text` event contents of the in-flight turn;
	// joined into the final reply at session.idle (no token delta stream).
	assistantParts []string
	// toolStartSeen tracks tool-call ids whose tool.start was already emitted, so a
	// re-delivered tool_use id never duplicates the tool.start node.
	toolStartSeen map[string]bool
	// lastUsage holds the most recent per-turn token counts captured from a
	// message.updated / step_finish frame; emitFinal reads+clears it (the terminal
	// session.idle frame carries no usage).
	lastUsage *domain.TokenUsage

	// mcpMu serializes opencode.json "mcp" read-modify-write cycles (mcp.go).
	mcpMu sync.Mutex

	// Pending sensing events buffered while busy.
	pendingEventsMu sync.Mutex
	pendingEvents   []pendingEvent

	// Run trackers (guard / broadcast / web_chat / silent / pose bucket).
	guardRunsMu sync.Mutex
	guardRuns   map[string]string

	broadcastRunsMu sync.Mutex
	broadcastRuns   map[string]bool

	webChatRunsMu sync.Mutex
	webChatRuns   map[string]bool

	silentRunsMu sync.Mutex
	silentRuns   map[string]bool

	// telegramRuns maps a Telegram-originated runID → originating chat id so
	// emitFinal DMs the reply back (see telegram_poll.go / translator.go).
	telegramRunsMu sync.Mutex
	telegramRuns   map[string]string

	// slackRuns maps a Slack-originated runID → its origin channel/thread so
	// emitFinal posts the reply back (see slack.go / translator.go).
	slackRunsMu sync.Mutex
	slackRuns   map[string]slackRun

	// discordRuns maps a Discord-originated runID → originating channel id so
	// emitFinal posts the reply back (see discord.go / translator.go).
	discordRunsMu sync.Mutex
	discordRuns   map[string]string

	// discordSession is the live gateway session handle (discord.go), guarded
	// so the reply sender works outside the handler goroutine.
	discordMu      sync.Mutex
	discordSession *discordgo.Session

	poseBucketRunsMu sync.Mutex
	poseBucketRuns   map[string]poseBucketInfo

	// Telegram coding-sessions (telegram_coding.go / coding_sessions.go): a chat
	// can attach to a folder's interactive `opencode` session and continue it over
	// Telegram (per-turn `opencode run --session <id> --dir <folder>`).
	codingMu     sync.Mutex
	codingSel    map[string]codingTarget
	codingList   map[string][]codingSession
	codingFolder map[string]*sync.Mutex

	// Coding-session test seams.
	codingEnvFilePath     string
	codingSelPath         string
	codingRunner          func(ctx context.Context, folder, threadID, prompt string) (reply, newThreadID string, err error)
	folderHasLiveOpenCode func(folder string) bool

	// Channel senders (Telegram Bot API): proactive alerts + reply DMs for
	// Telegram-originated turns.
	channels []domain.ChannelSender

	// Telegram inbound test seams (telegram_poll.go).
	telegramAPIBase     string
	telegramOffsetPath  string
	telegramTargetsPath string
	telegramSendTurn    func(text, reqID, runID string) error

	// Slack inbound test seams (slack.go / slack_sender.go).
	slackAPIBase  string
	slackSendTurn func(text, reqID, runID string) error

	// Discord inbound test seams (discord.go).
	discordSendTurn    func(text, reqID, runID string) error
	discordSendMessage func(channelID, text string) error

	// ackHookEnabled mirrors OpenClaw's emotion-acknowledge hook: when the device
	// declares the `expression` capability, every visible turn flashes a "thinking"
	// face before the reply lands.
	ackHookEnabled bool

	// Pending chat traces (idempotencyKey ↔ message text for MatchPendingByMessage).
	pendingChatMu  sync.Mutex
	pendingChatBuf []pendingTrace

	// Recent outbound texts (echo-suppression for session.message handler).
	recentOutboundMu    sync.Mutex
	recentOutboundTexts []recentOutbound
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

// ProvideService constructs the OpenCode service.
func ProvideService(cfg *config.Config, bus *monitor.Bus, sled *statusled.Service) *OpenCodeService {
	s := &OpenCodeService{
		config:         cfg,
		monitorBus:     bus,
		statusLED:      sled,
		guardRuns:      make(map[string]string),
		broadcastRuns:  make(map[string]bool),
		webChatRuns:    make(map[string]bool),
		silentRuns:     make(map[string]bool),
		telegramRuns:   make(map[string]string),
		slackRuns:      make(map[string]slackRun),
		discordRuns:    make(map[string]string),
		poseBucketRuns: make(map[string]poseBucketInfo),
	}
	s.channels = []domain.ChannelSender{
		&TelegramSender{svc: s},
		&SlackSender{svc: s},
	}
	s.ackHookEnabled = ackEmotionEnabled(cfg.DeviceTypeOrDefault())
	return s
}

// Name returns the display name surfaced via /api/openclaw/status.
func (s *OpenCodeService) Name() string { return "OpenCode" }

// IsReady reports whether the persistent WebSocket is currently connected.
func (s *OpenCodeService) IsReady() bool { return s.wsConnected.Load() }

// ConnectedAt returns the unix-seconds timestamp when the socket last connected.
func (s *OpenCodeService) ConnectedAt() int64 { return s.wsConnectedAt.Load() }

// AgentUptime — OpenCode does not report process uptime over the wire, so we
// have no value independent of the local WS reconnect cycle.
func (s *OpenCodeService) AgentUptime() int64 { return 0 }

// markOutboundChat / IsRecentOutboundChat mirror openclaw.OpenCodeService.
func (s *OpenCodeService) markOutboundChat(text string) {
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
func (s *OpenCodeService) IsRecentOutboundChat(text string) bool {
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

type pendingRun struct {
	sent  bool // Telemetry only: the outbound frame write succeeded.
	reqID string
	runID string
}
