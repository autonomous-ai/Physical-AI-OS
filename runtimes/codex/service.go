// Package codex implements domain.AgentGateway against the OpenAI Codex CLI
// reached over a persistent WebSocket to a thin local bridge.
package codex

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

// dispatchFn is the event sink shape shared by the read loop and wsDispatch.
// Named so atomic.Value always stores one concrete type.
type dispatchFn func(domain.WSEvent)

// Compile-time check: *CodexService implements domain.AgentGateway.
var _ domain.AgentGateway = (*CodexService)(nil)

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

// CodexService is the Codex backend implementation of domain.AgentGateway.
type CodexService struct {
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
	activeTurn        atomic.Bool
	busySince         atomic.Int64
	pendingMu         sync.Mutex
	pendingRuns       []pendingRun
	steeredRuns       []pendingRun // merged inputs awaiting the active turn's reply
	completedRequests []pendingRun
	sendChatMu        sync.Mutex
	currentRequestID  atomic.Value // string
	currentRunID      atomic.Value // string
	reqCounter        atomic.Int64
	// wsDispatch is the live connection's event sink, published by runWSConn so
	// paths OUTSIDE the read loop can still end a turn.
	wsDispatch atomic.Value // dispatchFn

	// Session state.
	sessionUUID atomic.Value // string

	// turnMu guards the per-turn accumulation state below.
	turnMu sync.Mutex
	// assistantParts collects agent_message texts of the in-flight turn; joined
	// into the final reply at turn.completed (codex exec does not stream deltas).
	assistantParts []string
	// toolStartSeen tracks item ids whose tool.start was already emitted, so an
	// item.completed without a prior item.started still yields a start+end pair.
	toolStartSeen map[string]bool

	// lastContextTokens is the live context size (Responses-API input_tokens,
	// which already includes the cached prefix) reported by
	// the most recent turn.completed.
	lastContextTokens atomic.Int64

	// mcpMu serializes config.toml [mcp_servers] read-modify-write cycles (mcp.go).
	mcpMu sync.Mutex

	// Pending sensing events buffered while busy.
	pendingEventsMu      sync.Mutex
	pendingEventsDrainMu sync.Mutex // serialize detach/defer/requeue across reconnect and speaker callbacks
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
	// can attach to a folder's interactive `codex` thread and continue it over
	// Telegram (per-turn `codex exec resume` in the folder's cwd).
	codingMu     sync.Mutex
	codingSel    map[string]codingTarget
	codingList   map[string][]codingSession
	codingFolder map[string]*sync.Mutex

	// Coding-session test seams.
	codexSessionsDirPath string
	codingEnvFilePath    string
	codingSelPath        string
	codingRunner         func(ctx context.Context, folder, threadID, prompt string) (reply, newThreadID string, err error)
	folderHasLiveCodex   func(folder string) bool

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

type pendingRun struct {
	sent  bool // Telemetry only: the outbound frame write succeeded.
	reqID string
	runID string
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

// ProvideService constructs the Codex service.
func ProvideService(cfg *config.Config, bus *monitor.Bus, sled *statusled.Service) *CodexService {
	s := &CodexService{
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
func (s *CodexService) Name() string { return "Codex" }

// IsReady reports whether the persistent WebSocket is currently connected.
func (s *CodexService) IsReady() bool { return s.wsConnected.Load() }

// ConnectedAt returns the unix-seconds timestamp when the socket last connected.
func (s *CodexService) ConnectedAt() int64 { return s.wsConnectedAt.Load() }

// AgentUptime — Codex does not report process uptime over the wire, so we
// have no value independent of the local WS reconnect cycle.
func (s *CodexService) AgentUptime() int64 { return 0 }

// markOutboundChat / IsRecentOutboundChat mirror openclaw.CodexService.
func (s *CodexService) markOutboundChat(text string) {
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
func (s *CodexService) IsRecentOutboundChat(text string) bool {
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
