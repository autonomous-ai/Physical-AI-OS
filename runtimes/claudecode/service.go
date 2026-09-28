// Package claudecode implements domain.AgentGateway against Claude Code
// (the Anthropic CLI agent) reached over a persistent WebSocket to a thin local
// bridge.
package claudecode

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
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/statusled"
)

// Compile-time check: *ClaudeCodeService implements domain.AgentGateway.
var _ domain.AgentGateway = (*ClaudeCodeService)(nil)

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

// ClaudeCodeService is the Claude Code backend implementation of domain.AgentGateway.
type ClaudeCodeService struct {
	config     *config.Config
	monitorBus *monitor.Bus
	statusLED  *statusled.Service

	// Persistent WebSocket.
	wsMu           sync.Mutex
	wsConn         *websocket.Conn
	wsConnected    atomic.Bool
	wsConnectedAt  atomic.Int64 // unix seconds when the socket last became ready
	wsHasConnected atomic.Bool  // skip "reconnect" TTS on first successful connect

	// Turn lifecycle.
	activeTurn           atomic.Bool
	busySince            atomic.Int64
	pendingMu            sync.Mutex
	pendingRuns          []pendingRun
	currentRequestID     atomic.Value // string
	sendChatMu           sync.Mutex
	pendingEventsDrainMu sync.Mutex
	currentRunID         atomic.Value // string
	reqCounter           atomic.Int64

	// Session state.
	sessionUUID atomic.Value // string

	// lastAssistantText is the latest assistant text block of the in-flight
	// turn — the fallback final text when result.result is empty (translator.go).
	lastAssistantText atomic.Value // string

	// mcpMu serializes workspace/.mcp.json read-modify-write cycles (mcp.go).
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

	poseBucketRunsMu sync.Mutex
	poseBucketRuns   map[string]poseBucketInfo

	// telegramRuns maps a Telegram-originated runID → originating chat id so
	// emitFinal DMs the reply back (see telegram_poll.go / translator.go).
	telegramRunsMu sync.Mutex
	telegramRuns   map[string]string

	// discordRuns maps a Discord-originated runID → originating channel id so
	// emitFinal posts the reply back (see discord.go / translator.go).
	discordRunsMu sync.Mutex
	discordRuns   map[string]string

	// discordSession is the live gateway session handle (discord.go), guarded
	// by discordMu so emitFinal's reply sender sees a consistent value.
	discordMu      sync.Mutex
	discordSession *discordgo.Session

	// Discord inbound test seams (discord.go).
	discordSendTurn    func(text, reqID, runID string) error
	discordSendMessage func(channelID, text string) error

	// Telegram inbound test seams (telegram_poll.go).
	telegramAPIBase     string
	telegramOffsetPath  string
	telegramTargetsPath string
	telegramSendTurn    func(text, reqID, runID string) error

	// slackRuns maps a Slack-originated runID → its origin channel/thread so
	// emitFinal posts the reply back (see slack.go / translator.go).
	slackRunsMu sync.Mutex
	slackRuns   map[string]slackRun

	// Slack inbound test seams (slack.go / slack_sender.go).
	slackAPIBase  string
	slackSendTurn func(text, reqID, runID string) error

	// Telegram coding-sessions (telegram_coding.go / coding_sessions.go): a chat
	// can attach to a folder's interactive `claude` session and continue it over
	// Telegram (per-turn --resume in the folder's cwd).
	codingMu     sync.Mutex
	codingSel    map[string]codingTarget
	codingList   map[string][]codingSession
	codingFolder map[string]*sync.Mutex

	// Coding-session test seams.
	claudeProjectsDirPath string
	codingEnvFilePath     string
	codingSelPath         string
	codingRunner          func(ctx context.Context, folder, sessionID, prompt string) (reply, newSessionID string, err error)
	folderHasLiveClaude   func(folder string) bool

	// Channel senders (Telegram, Slack).
	channels []domain.ChannelSender

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

// ProvideService constructs the Claude Code service.
func ProvideService(cfg *config.Config, bus *monitor.Bus, sled *statusled.Service) *ClaudeCodeService {
	s := &ClaudeCodeService{
		config:         cfg,
		monitorBus:     bus,
		statusLED:      sled,
		guardRuns:      make(map[string]string),
		broadcastRuns:  make(map[string]bool),
		webChatRuns:    make(map[string]bool),
		silentRuns:     make(map[string]bool),
		poseBucketRuns: make(map[string]poseBucketInfo),
		slackRuns:      make(map[string]slackRun),
	}
	s.channels = []domain.ChannelSender{
		&TelegramSender{svc: s},
		&SlackSender{svc: s},
	}
	s.ackHookEnabled = ackEmotionEnabled(cfg.DeviceTypeOrDefault())
	return s
}

// Name returns the display name surfaced via /api/openclaw/status.
func (s *ClaudeCodeService) Name() string { return "Claude Code" }

// IsReady reports whether the persistent WebSocket is currently connected.
func (s *ClaudeCodeService) IsReady() bool { return s.wsConnected.Load() }

// ConnectedAt returns the unix-seconds timestamp when the socket last connected.
func (s *ClaudeCodeService) ConnectedAt() int64 { return s.wsConnectedAt.Load() }

// AgentUptime — Claude Code does not report process uptime over the wire, so we
// have no value independent of the local WS reconnect cycle.
func (s *ClaudeCodeService) AgentUptime() int64 { return 0 }

// markOutboundChat / IsRecentOutboundChat mirror openclaw.ClaudeCodeService.
func (s *ClaudeCodeService) markOutboundChat(text string) {
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
func (s *ClaudeCodeService) IsRecentOutboundChat(text string) bool {
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
