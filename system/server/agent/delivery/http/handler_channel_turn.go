package http

import (
	"fmt"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gin-gonic/gin"

	migratepersona "go.autonomous.ai/os/system/agent/migrate_persona"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
)

// channelTurnRequest is the payload POSTed by runtimes/hermes/hooks/os-server-observer for every gateway turn.
type channelTurnRequest struct {
	Event   string `json:"event"` // "agent:start" | "agent:end"
	Context struct {
		Platform  string `json:"platform"` // telegram | slack | discord | api_server | cli | …
		UserID    string `json:"user_id"`
		ChatID    string `json:"chat_id"`
		ThreadID  string `json:"thread_id"`
		ChatType  string `json:"chat_type"` // dm | group | forum
		SessionID string `json:"session_id"`
		Message   string `json:"message"`  // inbound user text (agent:start); gateway truncates to 500
		Response  string `json:"response"` // assistant reply (agent:end); gateway truncates to 500
	} `json:"context"`
}

// channelHookSkipPlatforms are turns os-server already logs via sendChat (api_server, cli, pico);
// skipping them prevents double-counting. Matched case-insensitively, separators stripped.
var channelHookSkipPlatforms = map[string]bool{
	"apiserver": true,
	"api":       true,
	"cli":       true,
	"terminal":  true,
	"pico":      true,
}

// skipPlatform reports whether a platform's turns are device-originated and already logged.
func skipPlatform(platform string) bool {
	norm := strings.NewReplacer("_", "", "-", "", " ", "").Replace(strings.ToLower(platform))
	return norm == "" || channelHookSkipPlatforms[norm]
}

const channelTurnTTL = 10 * time.Minute

// channelHookTracker pairs a turn's agent:start with agent:end under one run_id, keyed by
// session_id (at most one open turn per session).
type channelHookTracker struct {
	mu   sync.Mutex
	open map[string]openChannelTurn
	seq  atomic.Uint64
}

type openChannelTurn struct {
	runID     string
	startedAt time.Time
}

var channelHook = &channelHookTracker{open: make(map[string]openChannelTurn)}

func (t *channelHookTracker) start(platform, sessionID string) string {
	runID := fmt.Sprintf("chan-%s-%d", platform, t.seq.Add(1))
	t.mu.Lock()
	t.pruneLocked()
	t.open[sessionID] = openChannelTurn{runID: runID, startedAt: time.Now()}
	t.mu.Unlock()
	return runID
}

// end returns the run_id paired with this session's open start, or a fresh id
// when the start was missed (e.g. os-server restarted mid-turn).
func (t *channelHookTracker) end(platform, sessionID string) string {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.pruneLocked()
	if o, ok := t.open[sessionID]; ok {
		delete(t.open, sessionID)
		return o.runID
	}
	return fmt.Sprintf("chan-%s-%d", platform, t.seq.Add(1))
}

func (t *channelHookTracker) pruneLocked() {
	cutoff := time.Now().Add(-channelTurnTTL)
	for k, v := range t.open {
		if v.startedAt.Before(cutoff) {
			delete(t.open, k)
		}
	}
}

// ChannelTurn receives Hermes gateway observer hook notifications and emits Flow Monitor events
// for messaging-channel turns, firing [HW:] markers on agent:end. Loopback-only.
func (h *AgentHandler) ChannelTurn(c *gin.Context) {
	// Always ACK 200: a hook error must not make the gateway retry or stall the turn.
	defer c.JSON(http.StatusOK, gin.H{"status": 1, "data": nil, "message": nil})

	var req channelTurnRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		return
	}
	ctx := req.Context
	if skipPlatform(ctx.Platform) {
		return
	}

	sessionID := ctx.SessionID
	if sessionID == "" {
		sessionID = ctx.Platform + ":" + ctx.ChatID
	}
	sender := ctx.UserID

	switch req.Event {
	case "agent:start":
		runID := channelHook.start(ctx.Platform, sessionID)
		// Fire the "thinking" ack for gateway-owned channel turns (ChannelStartEmotioner).
		if e, ok := h.agentGateway.(domain.ChannelStartEmotioner); ok {
			e.FireChannelStartEmotion(ctx.Message, runID)
		}
		flow.Log("chat_input", map[string]any{
			"run_id":  runID,
			"source":  "channel",
			"channel": ctx.Platform,
			"sender":  sender,
			"message": ctx.Message,
		}, runID)
		// Synthesise lifecycle_start so the AGENT pipeline node lights up.
		lcStart := map[string]any{"run_id": runID, "source": "channel_hook"}
		// Memory fingerprint (sizes + sha8, no content).
		if st := migratepersona.MemoryState(); st != nil {
			lcStart["memory"] = st
		}
		flow.Log("lifecycle_start", lcStart, runID)
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "chat_input",
			Summary: "[" + ctx.Platform + ":" + sender + "] " + channelTurnPreview(ctx.Message, 200),
			RunID:   runID,
			Detail:  map[string]string{"role": "user", "message": ctx.Message, "sender": sender, "channel": ctx.Platform},
		})

	case "agent:end":
		runID := channelHook.end(ctx.Platform, sessionID)
		// Logged as tts_suppressed (the reply went to the channel, not the speaker); that node is what
		// persists as the turn's response in JSONL.
		flow.Log("lifecycle_end", map[string]any{
			"run_id": runID,
			"source": "channel_hook",
		}, runID)

		// Fire [HW:] markers so channel turns drive local hardware. CAVEAT: the gateway truncates
		// ctx.Response (~500 chars), which can clip end-of-reply markers.
		raw := prunedImageMarkerRe.ReplaceAllString(ctx.Response, "")
		hwCalls, cleanText := extractHWCalls(raw)
		cleanText = extractSayTag(cleanText)
		cleanText = sanitizeAgentText(cleanText)
		h.fireHWCalls(hwCalls, runID)

		switch {
		case isAgentNoReply(cleanText):
			flow.Log("no_reply", map[string]any{"run_id": runID}, runID)
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "chat_response",
				Summary: "[no reply]",
				RunID:   runID,
				State:   "final",
				Detail:  map[string]string{"role": "assistant", "message": "[no reply]", "channel": ctx.Platform},
			})
		case strings.TrimSpace(cleanText) == "":
			// Markers but no spoken text — HW fired, still a valid turn (not no_reply).
			if len(hwCalls) > 0 {
				flow.Log("hw_only_reply", map[string]any{"run_id": runID}, runID)
			} else {
				flow.Log("no_reply", map[string]any{"run_id": runID}, runID)
				h.monitorBus.Push(domain.MonitorEvent{
					Type:    "chat_response",
					Summary: "[no reply]",
					RunID:   runID,
					State:   "final",
					Detail:  map[string]string{"role": "assistant", "message": "[no reply]", "channel": ctx.Platform},
				})
			}
		default:
			// Marker-stripped text so the UI bubble has no raw [HW:...].
			flow.Log("tts_suppressed", map[string]any{
				"run_id": runID,
				"reason": "channel_run",
				"text":   cleanText,
			}, runID)
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "chat_response",
				Summary: channelTurnPreview(cleanText, 200),
				RunID:   runID,
				State:   "final",
				Detail:  map[string]string{"role": "assistant", "message": cleanText, "channel": ctx.Platform},
			})
		}
	}
}

func channelTurnPreview(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "…"
}
