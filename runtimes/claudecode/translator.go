package claudecode

import (
	"encoding/json"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/telemetry"
)

// nowUnixMs returns the current time in milliseconds (matches the OpenClaw frame
// timestamp convention).
func nowUnixMs() int64 { return time.Now().UnixMilli() }

// claudeEvent is one inbound frame from the bridge: either a Claude Code
// stream-json event forwarded verbatim (type: system / assistant / user /
// result / stream_event) or a bridge-native frame (pong / bridge.status /
// bridge.error).
type claudeEvent struct {
	Type      string `json:"type"`
	Subtype   string `json:"subtype"`
	SessionID string `json:"session_id"`

	// assistant / user events
	Message *claudeMessage `json:"message"`

	// result events
	IsError  bool         `json:"is_error"`
	Result   string       `json:"result"`
	Usage    *claudeUsage `json:"usage"`
	NumTurns int          `json:"num_turns"`

	// bridge frames (bridge.status / bridge.error)
	Payload json.RawMessage `json:"payload"`
}

type claudeMessage struct {
	Role    string               `json:"role"`
	Content []claudeContentBlock `json:"content"`
}

// claudeContentBlock is one content block of an assistant/user message.
type claudeContentBlock struct {
	Type string `json:"type"`

	// text
	Text string `json:"text"`

	// tool_use
	ID    string          `json:"id"`
	Name  string          `json:"name"`
	Input json.RawMessage `json:"input"`

	// tool_result
	ToolUseID string          `json:"tool_use_id"`
	Content   json.RawMessage `json:"content"`
	IsError   bool            `json:"is_error"`
}

// claudeUsage is the token usage block on the result event (Anthropic API
// usage shape, per-turn).
type claudeUsage struct {
	InputTokens              int `json:"input_tokens"`
	CacheCreationInputTokens int `json:"cache_creation_input_tokens"`
	CacheReadInputTokens     int `json:"cache_read_input_tokens"`
	OutputTokens             int `json:"output_tokens"`
}

func (u *claudeUsage) toDomain() *domain.TokenUsage {
	if u == nil {
		return nil
	}
	total := u.InputTokens + u.CacheCreationInputTokens + u.CacheReadInputTokens + u.OutputTokens
	if total == 0 {
		return nil
	}
	return &domain.TokenUsage{
		InputTokens:      u.InputTokens,
		OutputTokens:     u.OutputTokens,
		CacheReadTokens:  u.CacheReadInputTokens,
		CacheWriteTokens: u.CacheCreationInputTokens,
		TotalTokens:      total,
	}
}

// translateFrame parses one inbound bridge frame and emits 0..N domain.WSEvent
// frames into dispatch.
func (s *ClaudeCodeService) translateFrame(raw []byte, dispatch func(domain.WSEvent)) {
	var f claudeEvent
	if err := json.Unmarshal(raw, &f); err != nil {
		slog.Debug("claudecode: non-JSON frame, ignored", "component", "claudecode", "raw", truncRunes(string(raw), 200))
		return
	}

	if f.SessionID != "" && f.SessionID != s.GetSessionKey() {
		s.SetSessionKey(f.SessionID)
	}

	switch f.Type {
	case "system":
		if f.Subtype == "init" {
			slog.Info("claudecode <<< session init", "component", "claudecode", "sessionKey", f.SessionID)
		}
	case "assistant":
		s.ensureTurnStarted(dispatch)
		s.handleAssistant(f, dispatch)
	case "user":
		// Tool results echo back as user messages.
		// A turn must already be open (the tool_use opened it); don't force one for unrelated user
		// echoes.
		s.handleToolResults(f, dispatch)
	case "result":
		s.ensureTurnStarted(dispatch)
		if f.IsError || (f.Subtype != "" && f.Subtype != "success") {
			s.handleError(errorText(f), dispatch)
		} else {
			s.emitFinal(f, dispatch)
		}
	case "bridge.error":
		s.ensureTurnStarted(dispatch)
		s.handleError(bridgeErrorText(f.Payload), dispatch)
	case "stream_event", "pong", "bridge.status":
	default:
		slog.Debug("claudecode: unhandled frame type", "component", "claudecode", "type", f.Type)
	}
}

// handleAssistant surfaces tool_use blocks as tool.start events and stashes the
// latest text block as the fallback final answer (the authoritative final text
// arrives on the result event).
func (s *ClaudeCodeService) handleAssistant(f claudeEvent, dispatch func(domain.WSEvent)) {
	if f.Message == nil {
		return
	}
	runID := s.getCurrentRunID()
	for _, blk := range f.Message.Content {
		switch blk.Type {
		case "text":
			if strings.TrimSpace(blk.Text) != "" {
				s.lastAssistantText.Store(blk.Text)
			}
		case "tool_use":
			name := blk.Name
			if name == "" {
				name = "tool"
			}
			slog.Info("claudecode <<< tool CALL", "component", "claudecode",
				"runID", runID, "tool", name, "toolCallId", blk.ID, "argsLen", len(blk.Input))
			startPayload, _ := json.Marshal(map[string]any{
				"runId":      runID,
				"sessionKey": s.GetSessionKey(),
				"stream":     "tool",
				"data": map[string]any{
					"phase":      "start",
					"name":       name,
					"toolCallId": blk.ID,
					"arguments":  string(blk.Input),
				},
			})
			dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: startPayload})
		}
	}
}

// handleToolResults closes tool traces: each tool_result block inside a user
// message maps to a tool.end for the tool_use that opened it.
func (s *ClaudeCodeService) handleToolResults(f claudeEvent, dispatch func(domain.WSEvent)) {
	if f.Message == nil || s.getCurrentRunID() == "" {
		return
	}
	runID := s.getCurrentRunID()
	for _, blk := range f.Message.Content {
		if blk.Type != "tool_result" {
			continue
		}
		endPayload, _ := json.Marshal(map[string]any{
			"runId":      runID,
			"sessionKey": s.GetSessionKey(),
			"stream":     "tool",
			"data": map[string]any{
				"phase":      "end",
				"toolCallId": blk.ToolUseID,
				"result":     toolResultText(blk.Content),
			},
		})
		dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: endPayload})
	}
}

// toolResultText extracts a printable string from a tool_result content field,
// which Claude sends either as a plain string or as content blocks
// ([{type:"text",text:"..."}]).
func toolResultText(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var str string
	if json.Unmarshal(raw, &str) == nil {
		return str
	}
	var blocks []struct {
		Text string `json:"text"`
	}
	if json.Unmarshal(raw, &blocks) == nil && len(blocks) > 0 {
		parts := make([]string, 0, len(blocks))
		for _, b := range blocks {
			if b.Text != "" {
				parts = append(parts, b.Text)
			}
		}
		if len(parts) > 0 {
			return strings.Join(parts, " ")
		}
	}
	return string(raw)
}

// errorText resolves the message for an error-shaped result event.
func errorText(f claudeEvent) string {
	if strings.TrimSpace(f.Result) != "" {
		return f.Result
	}
	if f.Subtype != "" {
		return "claude code result: " + f.Subtype
	}
	return "claude code error"
}

// bridgeErrorText pulls the message out of a bridge.error payload.
func bridgeErrorText(raw json.RawMessage) string {
	var p struct {
		Message string `json:"message"`
	}
	if json.Unmarshal(raw, &p) == nil && p.Message != "" {
		return p.Message
	}
	return "claudecode bridge error"
}

// ensureTurnStarted emits lifecycle.start exactly once per turn.
func (s *ClaudeCodeService) ensureTurnStarted(dispatch func(domain.WSEvent)) {
	if s.getCurrentRunID() != "" {
		return
	}
	runID := s.consumePendingRunID()
	if runID == "" {
		_, runID = s.NextChatRunID()
	}
	s.setCurrentRunID(runID)
	if !s.activeTurn.Load() {
		s.busySince.Store(nowUnixMs())
		s.activeTurn.Store(true)
	}

	slog.Info("claudecode <<< turn started", "component", "claudecode", "runID", runID, "sessionKey", s.GetSessionKey())
	payload, _ := json.Marshal(map[string]any{
		"runId":      runID,
		"sessionKey": s.GetSessionKey(),
		"stream":     "lifecycle",
		"data": map[string]any{
			"phase":     "start",
			"startedAt": nowUnixMs(),
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
}

// emitFinal emits (a) the final chat message and (b) lifecycle.end with usage,
// then closes the turn.
// Turn ids are reset BEFORE dispatch: the consumer starts the next turn synchronously.
func (s *ClaudeCodeService) emitFinal(f claudeEvent, dispatch func(domain.WSEvent)) {
	runID := s.getCurrentRunID()
	finalText := f.Result
	if strings.TrimSpace(finalText) == "" {
		// result.result is normally the final assistant text; fall back to the
		// last assistant text block when it is empty (defensive).
		finalText, _ = s.lastAssistantText.Load().(string)
	}

	logArgs := []any{
		"component", "claudecode",
		"runID", runID,
		"finalLen", len(finalText),
		"final", truncRunes(finalText, 500),
		"numTurns", f.NumTurns,
	}
	if f.Usage != nil {
		logArgs = append(logArgs, "inputTokens", f.Usage.InputTokens, "outputTokens", f.Usage.OutputTokens)
	}
	slog.Info("claudecode <<< final answer", logArgs...)

	s.finishCurrentCorrelation()

	s.lastAssistantText.Store("")

	// Slack-originated turn (slack.go): post the reply back to the originating
	// channel/thread (chat.postMessage) and clear the eyes ack reaction.
	// Best-effort in a goroutine: the read loop must not block on the Web API.
	if o, ok := s.consumeSlackRun(runID); ok {
		go s.finishSlackTurn(o, stripForChannel(finalText))
	}

	// Telegram-originated turn (telegram_poll.go): DM the reply back to the
	// originating chat.
	// Best-effort in a goroutine: the read loop must not block on the Bot API.
	if chatID := s.consumeTelegramRun(runID); chatID != "" && finalText != "" {
		if reply := stripForChannel(finalText); reply != "" {
			go func() {
				if err := s.SendToUser(chatID, reply, ""); err != nil {
					slog.Error("telegram reply send failed",
						"component", "claudecode", "runID", runID, "chatID", chatID, "error", err)
				}
			}()
		}
	}

	// Discord-originated turn (discord.go): post the reply back to the
	// originating channel (chunked at Discord's 2000-char limit).
	// Best-effort in a goroutine: the read loop must not block on the Discord API.
	if channelID := s.consumeDiscordRun(runID); channelID != "" && finalText != "" {
		go s.finishDiscordTurn(channelID, stripForChannel(finalText))
	}

	// The whole reply as a single assistant delta BEFORE chat.final — the
	// shared consumer only flushes TTS + [HW:/…] markers (and logs tts_send,
	// which the web chat reads) from accumulated deltas at lifecycle.end;
	if finalText != "" {
		deltaPayload, _ := json.Marshal(map[string]any{
			"runId":      runID,
			"sessionKey": s.GetSessionKey(),
			"stream":     "assistant",
			"data":       map[string]any{"delta": finalText},
		})
		dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: deltaPayload})
	}

	chatMsg, _ := json.Marshal(map[string]any{
		"runId":      runID,
		"sessionKey": s.GetSessionKey(),
		"state":      "final",
		"role":       "assistant",
		"message":    finalText,
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "chat", Payload: chatMsg})

	endPayload, _ := json.Marshal(map[string]any{
		"runId":      runID,
		"sessionKey": s.GetSessionKey(),
		"stream":     "lifecycle",
		"data": map[string]any{
			"phase":   "end",
			"endedAt": nowUnixMs(),
			"usage":   f.Usage.toDomain(),
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: endPayload})
}

func (s *ClaudeCodeService) handleError(msg string, dispatch func(domain.WSEvent)) {
	runID := s.getCurrentRunID()
	if msg == "" {
		msg = "claudecode error"
	}
	slog.Warn("claudecode <<< error", "component", "claudecode", "runID", runID, "error", truncRunes(msg, 500))

	s.finishCurrentCorrelation()

	s.lastAssistantText.Store("")

	// Telegram-originated turn: consume the tracker so the map doesn't leak
	// (and the typing keeper stops).
	if chatID := s.consumeTelegramRun(runID); chatID != "" {
		slog.Warn("telegram-originated turn failed — reply dropped",
			"component", "claudecode", "runID", runID, "chatID", chatID)
	}

	if o, ok := s.consumeSlackRun(runID); ok {
		slog.Warn("slack-originated turn failed — reply dropped",
			"component", "claudecode", "runID", runID, "channel", o.channel)
		go s.finishSlackTurn(o, "")
	}

	// Discord-originated turn: consume the tracker so the map doesn't leak
	// (and the typing keeper stops).
	if channelID := s.consumeDiscordRun(runID); channelID != "" {
		slog.Warn("discord-originated turn failed — reply dropped",
			"component", "claudecode", "runID", runID, "channelID", channelID)
	}

	payload, _ := json.Marshal(map[string]any{
		"runId":      runID,
		"sessionKey": s.GetSessionKey(),
		"stream":     "lifecycle",
		"data": map[string]any{
			"phase":   "error",
			"error":   msg,
			"endedAt": nowUnixMs(),
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
}

func (s *ClaudeCodeService) getCurrentRunID() string {
	v, _ := s.currentRunID.Load().(string)
	return v
}

func (s *ClaudeCodeService) setCurrentRunID(runID string) { s.currentRunID.Store(runID) }

func (s *ClaudeCodeService) addPendingRun(reqID, runID string) {
	s.pendingMu.Lock()
	defer s.pendingMu.Unlock()
	s.pendingRuns = append(s.pendingRuns, pendingRun{reqID: reqID, runID: runID})
}

func (s *ClaudeCodeService) hasPendingRuns() bool {
	s.pendingMu.Lock()
	defer s.pendingMu.Unlock()
	return len(s.pendingRuns) > 0
}

func (s *ClaudeCodeService) takePendingRun(reqID, runID string, fifo bool) pendingRun {
	s.pendingMu.Lock()
	defer s.pendingMu.Unlock()
	for i, pending := range s.pendingRuns {
		if fifo || (reqID != "" && pending.reqID == reqID && (runID == "" || pending.runID == runID)) || (reqID == "" && runID != "" && pending.runID == runID) {
			s.pendingRuns = append(s.pendingRuns[:i], s.pendingRuns[i+1:]...)
			return pending
		}
	}
	return pendingRun{}
}

func (s *ClaudeCodeService) removePendingRun(reqID string) { s.takePendingRun(reqID, "", false) }

func (s *ClaudeCodeService) consumePendingRunID() string {
	pending := s.takePendingRun("", "", true)
	s.currentRequestID.Store(pending.reqID)
	return pending.runID
}

func (s *ClaudeCodeService) finishCurrentCorrelation() {
	s.currentRequestID.Store("")
	s.currentRunID.Store("")
}

// clearTurn resets the in-flight turn ids without touching busy state.
func (s *ClaudeCodeService) clearTurn() {
	telemetry.ReportTaskObservationLost(s.unfinishedTaskRunIDs()...)
	s.pendingMu.Lock()
	s.pendingRuns = nil
	s.pendingMu.Unlock()
	s.currentRequestID.Store("")
	s.currentRunID.Store("")

	s.lastAssistantText.Store("")
}

// markPendingRunSent records transport evidence without changing admission or correlation.
func (s *ClaudeCodeService) markPendingRunSent(reqID string) {
	s.pendingMu.Lock()
	defer s.pendingMu.Unlock()
	for i := range s.pendingRuns {
		if s.pendingRuns[i].reqID == reqID {
			s.pendingRuns[i].sent = true
			return
		}
	}
}

// unfinishedTaskRunIDs excludes unsent attempts and normally completed turns.
// Consumers preserve authoritative terminals if a timeout races completion.
func (s *ClaudeCodeService) unfinishedTaskRunIDs() []string {
	var ids []string
	if current := s.getCurrentRunID(); current != "" {
		ids = append(ids, current)
	}
	s.pendingMu.Lock()
	defer s.pendingMu.Unlock()
	for _, pending := range s.pendingRuns {
		if pending.sent {
			ids = append(ids, pending.runID)
		}
	}
	return ids
}
