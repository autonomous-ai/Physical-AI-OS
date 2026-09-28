package codex

import (
	"encoding/json"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/telemetry"
)

// nowUnixMs returns the current time in milliseconds (matches the OpenClaw frame
// timestamp convention).
func nowUnixMs() int64 { return time.Now().UnixMilli() }

// codexFrame is one inbound frame from the bridge: either a Codex exec JSONL
// event forwarded verbatim (thread.started / turn.* / item.*) or a bridge
// frame (bridge.status / bridge.error / pong).
type codexFrame struct {
	Type      string     `json:"type"`
	RequestID string     `json:"request_id"`
	RunID     string     `json:"run_id"`
	ThreadID  string     `json:"thread_id"` // thread.started, bridge.status
	Item      codexItem  `json:"item"`      // item.started / item.completed
	Usage     *codexUse  `json:"usage"`     // turn.completed
	Error     codexError `json:"error"`     // turn.failed (object), bridge.error (string) — see UnmarshalJSON
	Message   string     `json:"message"`   // error events
	State     string     `json:"state"`     // bridge.status
}

// codexError tolerates both shapes seen on the wire: turn.failed carries an
// object {"message": "..."}, bridge.error carries a plain string.
type codexError struct {
	Message string
}

func (e *codexError) UnmarshalJSON(raw []byte) error {
	var s string
	if json.Unmarshal(raw, &s) == nil {
		e.Message = s
		return nil
	}
	var obj struct {
		Message string `json:"message"`
	}
	if json.Unmarshal(raw, &obj) == nil {
		e.Message = obj.Message
	}
	return nil
}

// codexItem is one output item of a turn.
type codexItem struct {
	ID       string `json:"id"`
	ItemType string `json:"item_type"`
	Type     string `json:"type"`

	// agent_message / reasoning
	Text string `json:"text"`

	// command_execution
	Command          string `json:"command"`
	AggregatedOutput string `json:"aggregated_output"`
	ExitCode         *int   `json:"exit_code"`

	// mcp_tool_call
	Server string `json:"server"`
	Tool   string `json:"tool"`

	// web_search
	Query string `json:"query"`

	// file_change
	Changes json.RawMessage `json:"changes"`

	Status string `json:"status"`
}

func (it codexItem) kind() string {
	if it.ItemType != "" {
		return it.ItemType
	}
	return it.Type
}

// codexUse is the turn.completed usage block.
type codexUse struct {
	InputTokens          int `json:"input_tokens"`
	CachedInputTokens    int `json:"cached_input_tokens"`
	CacheWriteInputToken int `json:"cache_write_input_tokens"`
	OutputTokens         int `json:"output_tokens"`
}

// toDomain maps Codex usage onto domain.TokenUsage.
// The live context size for rotation is NOT read from here — translateFrame stashes the raw
// input_tokens into s.lastContextTokens (see rotation.go).
func (u *codexUse) toDomain() *domain.TokenUsage {
	if u == nil {
		return nil
	}
	if u.InputTokens == 0 && u.CachedInputTokens == 0 && u.OutputTokens == 0 {
		return nil
	}
	in, cached := u.InputTokens, u.CachedInputTokens
	if cached > 0 && cached <= in {
		in -= cached
	} else if cached > in {
		// Shape we have never seen: trust the total, drop the cache split
		// rather than report a negative fresh-input figure.
		cached = 0
	}
	return &domain.TokenUsage{
		InputTokens:      in,
		OutputTokens:     u.OutputTokens,
		CacheReadTokens:  cached,
		CacheWriteTokens: u.CacheWriteInputToken,
		TotalTokens:      in + cached + u.OutputTokens,
	}
}

// translateFrame parses one inbound bridge frame and emits 0..N domain.WSEvent
// frames into dispatch.
func (s *CodexService) translateFrame(raw []byte, dispatch func(domain.WSEvent)) {
	var f codexFrame
	if err := json.Unmarshal(raw, &f); err != nil {
		slog.Debug("codex: non-JSON frame, ignored", "component", "codex", "raw", truncRunes(string(raw), 200))
		return
	}

	if f.Type == "bridge.rejected" {
		s.rejectQueuedFrame(f, dispatch)
		return
	}
	if f.Type == "bridge.steered" {
		s.completeSteeredFrame(f, dispatch)
		return
	}
	if isTurnFrame(f.Type) && !s.adoptFrameCorrelation(f, dispatch) {
		return
	}
	// Ignored stale/interleaved frames must not change the active session.
	if f.ThreadID != "" && f.ThreadID != s.GetSessionKey() {
		s.SetSessionKey(f.ThreadID)
	}

	switch f.Type {
	case "thread.started", "turn.started":
		s.ensureTurnStarted(dispatch)
	case "item.started", "item.updated":
		s.handleItemStarted(f, dispatch)
	case "item.completed":
		s.handleItemCompleted(f, dispatch)
	case "turn.completed":
		s.emitFinal(f, dispatch)
	case "turn.failed":
		s.handleError(f.Error.Message, dispatch)
	case "error":
		msg := f.Message
		if msg == "" {
			msg = f.Error.Message
		}
		s.handleError(msg, dispatch)
	case "bridge.error":
		s.handleError(f.Error.Message, dispatch)
	case "bridge.status":
		slog.Info("codex bridge status", "component", "codex", "state", f.State, "threadId", f.ThreadID)
	case "pong":
	default:
		slog.Debug("codex: unhandled frame type", "component", "codex", "type", f.Type)
	}
}

// completeSteeredFrame links an accepted follow-up to its active model turn.
func (s *CodexService) completeSteeredFrame(f codexFrame, dispatch func(domain.WSEvent)) {
	pending := s.takePendingRun(f.RequestID, f.RunID, false)
	if pending.runID == "" {
		return
	}
	flow.Log("turn_merged", map[string]any{"parent_run_id": s.getCurrentRunID()}, pending.runID)
	if s.IsWebChatRun(pending.runID) || !s.IsSilentRun(pending.runID) {
		s.pendingMu.Lock()
		s.steeredRuns = append(s.steeredRuns, pending)
		s.pendingMu.Unlock()
		payload, _ := json.Marshal(map[string]any{
			"runId": pending.runID, "sessionKey": s.GetSessionKey(), "stream": "lifecycle",
			"data": map[string]any{"phase": "start", "startedAt": nowUnixMs(), "mergedIntoActiveTurn": true},
		})
		dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
		slog.Info("codex: follow-up merged into active turn; awaiting shared reply",
			"component", "codex", "request_id", pending.reqID, "runID", pending.runID)
		return
	}
	slog.Info("codex: follow-up merged into active turn", "component", "codex",
		"request_id", pending.reqID, "runID", pending.runID)
	payload, _ := json.Marshal(map[string]any{
		"runId": pending.runID, "sessionKey": s.GetSessionKey(), "stream": "lifecycle",
		"data": map[string]any{"phase": "end", "endedAt": nowUnixMs(), "mergedIntoActiveTurn": true},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
}

func (s *CodexService) takeSteeredRuns() []pendingRun {
	s.pendingMu.Lock()
	defer s.pendingMu.Unlock()
	runs := s.steeredRuns
	s.steeredRuns = nil
	return runs
}

func (s *CodexService) emitMergedFinal(runID, finalText string, dispatch func(domain.WSEvent)) {
	// A shared result must not replay hardware markers for every merged request.
	text := stripForChannel(finalText)
	if text != "" {
		payload, _ := json.Marshal(map[string]any{
			"runId": runID, "sessionKey": s.GetSessionKey(), "stream": "assistant",
			"data": map[string]any{"delta": text, "mergedIntoActiveTurn": true},
		})
		dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
	}
	chat, _ := json.Marshal(map[string]any{
		"runId": runID, "sessionKey": s.GetSessionKey(), "state": "final", "role": "assistant", "message": text,
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "chat", Payload: chat})
	end, _ := json.Marshal(map[string]any{
		"runId": runID, "sessionKey": s.GetSessionKey(), "stream": "lifecycle",
		"data": map[string]any{"phase": "end", "endedAt": nowUnixMs(), "mergedIntoActiveTurn": true},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: end})
}

// handleItemStarted opens the turn and surfaces tool.start for the item kinds
// that model a tool invocation.
func (s *CodexService) handleItemStarted(f codexFrame, dispatch func(domain.WSEvent)) {
	s.ensureTurnStarted(dispatch)
	switch f.Item.kind() {
	case "command_execution":
		// ensureToolStart (not emitToolStart): item.updated re-delivers the same
		// item id and must not duplicate the tool.start node.
		s.ensureToolStart(f.Item.ID, "shell", f.Item.Command, dispatch)
	case "mcp_tool_call":
		s.ensureToolStart(f.Item.ID, mcpToolName(f.Item), "", dispatch)
	}
}

// handleItemCompleted routes a finished item.
func (s *CodexService) handleItemCompleted(f codexFrame, dispatch func(domain.WSEvent)) {
	s.ensureTurnStarted(dispatch)
	it := f.Item
	switch it.kind() {
	case "agent_message":
		if strings.TrimSpace(it.Text) == "" {
			return
		}
		// Only the LAST agent_message of a turn is the reply; every earlier one is narration that
		// must never reach TTS.
		s.turnMu.Lock()
		var preamble string
		if n := len(s.assistantParts); n > 0 && !hasHWMarker(s.assistantParts[n-1]) {
			preamble = s.assistantParts[n-1]
			s.assistantParts = s.assistantParts[:n-1]
		}
		s.assistantParts = append(s.assistantParts, it.Text)
		s.turnMu.Unlock()
		if preamble != "" {
			s.emitThinking(preamble, dispatch)
		}
	case "command_execution":
		s.ensureToolStart(it.ID, "shell", it.Command, dispatch)
		s.emitToolEnd(it.ID, truncRunes(it.AggregatedOutput, 400), dispatch)
	case "mcp_tool_call":
		s.ensureToolStart(it.ID, mcpToolName(it), "", dispatch)
		s.emitToolEnd(it.ID, it.Status, dispatch)
	case "web_search":
		s.ensureToolStart(it.ID, "web_search", it.Query, dispatch)
		s.emitToolEnd(it.ID, "", dispatch)
	case "file_change":
		s.ensureToolStart(it.ID, "file_changes", string(it.Changes), dispatch)
		s.emitToolEnd(it.ID, it.Status, dispatch)
	case "reasoning", "todo_list":
	default:
		slog.Debug("codex: unhandled item kind", "component", "codex", "kind", it.kind())
	}
}

// hasHWMarker reports whether text carries an inline hardware marker, in
// either the plain form `[HW:/led/off]` or the markdown-link form
// `[Lights off](HW:/led/off)`.
func hasHWMarker(text string) bool {
	return strings.Contains(text, "[HW:") || strings.Contains(text, "](HW:")
}

// emitThinking surfaces text on the thinking stream, which the OS server routes
// to Flow Monitor only — never to TTS or a channel reply.
func (s *CodexService) emitThinking(text string, dispatch func(domain.WSEvent)) {
	runID := s.getCurrentRunID()
	slog.Info("codex <<< preamble demoted to thinking", "component", "codex",
		"runID", runID, "text", truncRunes(text, 200))
	payload, _ := json.Marshal(map[string]any{
		"runId":      runID,
		"sessionKey": s.GetSessionKey(),
		"stream":     "thinking",
		"data": map[string]any{
			"text": text,
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
}

func mcpToolName(it codexItem) string {
	name := it.Tool
	if it.Server != "" {
		name = it.Server + "." + it.Tool
	}
	if name == "" || name == "." {
		return "mcp_tool"
	}
	return name
}

// ensureTurnStarted emits lifecycle.start exactly once per turn.
func (s *CodexService) ensureTurnStarted(dispatch func(domain.WSEvent)) {
	if s.getCurrentRunID() != "" {
		return
	}
	runID := s.consumePendingRunID()
	if runID == "" {
		_, runID = s.NextChatRunID()
	}
	s.startTurn(runID, dispatch)
}

func (s *CodexService) startTurn(runID string, dispatch func(domain.WSEvent)) {
	s.setCurrentRunID(runID)
	s.busySince.Store(nowUnixMs())
	s.activeTurn.Store(true)
	s.turnMu.Lock()
	s.assistantParts = nil
	s.toolStartSeen = make(map[string]bool)
	s.turnMu.Unlock()

	slog.Info("codex <<< turn started", "component", "codex", "runID", runID, "threadId", s.GetSessionKey())
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

// ensureToolStart emits tool.start when it was not already emitted for this
// item id (an item.completed can arrive without a prior item.started).
func (s *CodexService) ensureToolStart(id, name, args string, dispatch func(domain.WSEvent)) {
	s.turnMu.Lock()
	seen := s.toolStartSeen != nil && s.toolStartSeen[id]
	s.turnMu.Unlock()
	if !seen {
		s.emitToolStart(id, name, args, dispatch)
	}
}

func (s *CodexService) emitToolStart(id, name, args string, dispatch func(domain.WSEvent)) {
	s.turnMu.Lock()
	if s.toolStartSeen == nil {
		s.toolStartSeen = make(map[string]bool)
	}
	s.toolStartSeen[id] = true
	s.turnMu.Unlock()

	runID := s.getCurrentRunID()
	slog.Info("codex <<< tool CALL", "component", "codex",
		"runID", runID, "tool", name, "toolCallId", id, "argsLen", len(args))
	payload, _ := json.Marshal(map[string]any{
		"runId":      runID,
		"sessionKey": s.GetSessionKey(),
		"stream":     "tool",
		"data": map[string]any{
			"phase":      "start",
			"name":       name,
			"toolCallId": id,
			"arguments":  args,
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
}

func (s *CodexService) emitToolEnd(id, result string, dispatch func(domain.WSEvent)) {
	payload, _ := json.Marshal(map[string]any{
		"runId":      s.getCurrentRunID(),
		"sessionKey": s.GetSessionKey(),
		"stream":     "tool",
		"data": map[string]any{
			"phase":      "end",
			"toolCallId": id,
			"result":     result,
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
}

// emitFinal emits, in order: (a) the whole reply as a single assistant delta,
// (b) the final chat message, (c) lifecycle.end with usage — then closes the
// turn. Turn ids are reset BEFORE dispatch: the consumer starts the next turn synchronously.
func (s *CodexService) emitFinal(f codexFrame, dispatch func(domain.WSEvent)) {
	s.ensureTurnStarted(dispatch)
	runID := s.getCurrentRunID()

	s.turnMu.Lock()
	finalText := strings.Join(s.assistantParts, "\n\n")
	s.assistantParts = nil
	s.toolStartSeen = nil
	s.turnMu.Unlock()

	logArgs := []any{
		"component", "codex",
		"runID", runID,
		"finalLen", len(finalText),
		"final", truncRunes(finalText, 500),
	}
	if f.Usage != nil {
		logArgs = append(logArgs,
			"inputTokens", f.Usage.InputTokens,
			"cachedInputTokens", f.Usage.CachedInputTokens,
			"outputTokens", f.Usage.OutputTokens)
		s.lastContextTokens.Store(int64(f.Usage.InputTokens))
	}
	slog.Info("codex <<< turn completed", logArgs...)

	s.finishCurrentCorrelation()
	steeredRuns := s.takeSteeredRuns()
	speechRunID := runID
	for _, merged := range steeredRuns {
		if !s.IsWebChatRun(merged.runID) && !s.IsSilentRun(merged.runID) {
			speechRunID = merged.runID
		}
	}
	if speechRunID != runID {
		if !s.IsWebChatRun(runID) {
			s.MarkSilentRun(runID)
		}
		for _, merged := range steeredRuns {
			if merged.runID != speechRunID && !s.IsWebChatRun(merged.runID) {
				s.MarkSilentRun(merged.runID)
			}
		}
	}

	// Telegram-originated turn (telegram_poll.go): DM the reply back to the
	// originating chat.
	// Best-effort in a goroutine: the read loop must not block on the Bot API.
	if chatID := s.consumeTelegramRun(runID); chatID != "" && finalText != "" {
		if reply := stripForChannel(finalText); reply != "" {
			go func() {
				if err := s.SendToUser(chatID, reply, ""); err != nil {
					slog.Error("telegram reply send failed",
						"component", "codex", "runID", runID, "chatID", chatID, "error", err)
				}
			}()
		}
	}

	// Slack-originated turn (slack.go): post the reply back to the originating
	// channel/thread (chat.postMessage) and clear the eyes ack reaction.
	// Best-effort in a goroutine: the read loop must not block on the Web API.
	if o, ok := s.consumeSlackRun(runID); ok {
		go s.finishSlackTurn(o, stripForChannel(finalText))
	}

	// Discord-originated turn (discord.go): post the reply back to the
	// originating channel (chunked at Discord's 2000-char limit).
	// Best-effort in a goroutine: the read loop must not block on the Discord API.
	if channelID := s.consumeDiscordRun(runID); channelID != "" && finalText != "" {
		go s.finishDiscordTurn(channelID, stripForChannel(finalText))
	}

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
	for _, merged := range steeredRuns {
		s.emitMergedFinal(merged.runID, finalText, dispatch)
	}
}

func (s *CodexService) handleError(msg string, dispatch func(domain.WSEvent)) {
	s.ensureTurnStarted(dispatch)
	runID := s.getCurrentRunID()
	if msg == "" {
		msg = "codex error"
	}
	slog.Warn("codex <<< error", "component", "codex", "runID", runID, "error", msg)

	s.finishCurrentCorrelation()
	steeredRuns := s.takeSteeredRuns()
	s.turnMu.Lock()
	s.assistantParts = nil
	s.toolStartSeen = nil
	s.turnMu.Unlock()

	// Telegram-originated turn: consume the tracker so the map doesn't leak.
	if chatID := s.consumeTelegramRun(runID); chatID != "" {
		slog.Warn("telegram-originated turn failed — reply dropped",
			"component", "codex", "runID", runID, "chatID", chatID)
	}

	if o, ok := s.consumeSlackRun(runID); ok {
		slog.Warn("slack-originated turn failed — reply dropped",
			"component", "codex", "runID", runID, "channel", o.channel)
		go s.finishSlackTurn(o, "")
	}

	// Discord-originated turn: consume the tracker so the map doesn't leak
	// (and the typing keeper stops).
	if channelID := s.consumeDiscordRun(runID); channelID != "" {
		slog.Warn("discord-originated turn failed — reply dropped",
			"component", "codex", "runID", runID, "channelID", channelID)
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
	s.emitMergedErrors(steeredRuns, msg, dispatch)
}

func (s *CodexService) emitMergedErrors(runs []pendingRun, msg string, dispatch func(domain.WSEvent)) {
	for _, run := range runs {
		payload, _ := json.Marshal(map[string]any{
			"runId": run.runID, "sessionKey": s.GetSessionKey(), "stream": "lifecycle",
			"data": map[string]any{"phase": "error", "error": msg, "endedAt": nowUnixMs(), "mergedIntoActiveTurn": true},
		})
		dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
	}
}

func (s *CodexService) getCurrentRunID() string {
	v, _ := s.currentRunID.Load().(string)
	return v
}

func (s *CodexService) setCurrentRunID(runID string) { s.currentRunID.Store(runID) }

func (s *CodexService) addPendingRun(reqID, runID string) {
	s.pendingMu.Lock()
	defer s.pendingMu.Unlock()
	s.pendingRuns = append(s.pendingRuns, pendingRun{reqID: reqID, runID: runID})
}

func (s *CodexService) hasPendingRuns() bool {
	s.pendingMu.Lock()
	defer s.pendingMu.Unlock()
	return len(s.pendingRuns) > 0
}

func (s *CodexService) takePendingRun(reqID, runID string, fifo bool) pendingRun {
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

func (s *CodexService) removePendingRun(reqID string) { s.takePendingRun(reqID, "", false) }

func (s *CodexService) consumePendingRunID() string {
	pending := s.takePendingRun("", "", true)
	s.currentRequestID.Store(pending.reqID)
	return pending.runID
}

func (s *CodexService) finishCurrentCorrelation() {
	reqID, _ := s.currentRequestID.Load().(string)
	if reqID != "" {
		s.pendingMu.Lock()
		s.completedRequests = append(s.completedRequests, pendingRun{reqID: reqID, runID: s.getCurrentRunID()})
		if len(s.completedRequests) > 256 {
			s.completedRequests = s.completedRequests[len(s.completedRequests)-256:]
		}
		s.pendingMu.Unlock()
	}
	s.currentRequestID.Store("")
	s.currentRunID.Store("")
}

func isTurnFrame(kind string) bool {
	switch kind {
	case "thread.started", "turn.started", "item.started", "item.updated", "item.completed", "turn.completed", "turn.failed", "error", "bridge.error":
		return true
	default:
		return false
	}
}

func (s *CodexService) adoptFrameCorrelation(f codexFrame, dispatch func(domain.WSEvent)) bool {
	if f.RequestID == "" && f.RunID == "" {
		return true
	}
	if current := s.getCurrentRunID(); current != "" {
		request, _ := s.currentRequestID.Load().(string)
		if (f.RunID == "" || f.RunID == current) && (f.RequestID == "" || request == "" || f.RequestID == request) {
			return true
		}
		slog.Warn("codex: ignoring interleaved turn frame", "request_id", f.RequestID, "run_id", f.RunID, "current_run", current)
		return false
	}
	s.pendingMu.Lock()
	completed := false
	for _, request := range s.completedRequests {
		if f.RequestID != "" && request.reqID == f.RequestID && (f.RunID == "" || request.runID == f.RunID) {
			completed = true
			break
		}
	}
	s.pendingMu.Unlock()
	if completed {
		return false
	}
	pending := s.takePendingRun(f.RequestID, f.RunID, false)
	runID := pending.runID
	if runID == "" {
		runID = f.RunID
	}
	if runID == "" {
		slog.Warn("codex: unmatched request frame", "request_id", f.RequestID)
		return false
	}
	requestID := f.RequestID
	if requestID == "" {
		requestID = pending.reqID
	}
	s.currentRequestID.Store(requestID)
	if f.ThreadID != "" && f.ThreadID != s.GetSessionKey() {
		s.SetSessionKey(f.ThreadID)
	}
	s.startTurn(runID, dispatch)
	return true
}

// Queue rejection is not a failure of the currently streaming turn.
func (s *CodexService) rejectQueuedFrame(f codexFrame, dispatch func(domain.WSEvent)) {
	pending := s.takePendingRun(f.RequestID, f.RunID, false)
	if pending.runID == "" {
		return
	}
	payload, _ := json.Marshal(map[string]any{
		"runId": pending.runID, "sessionKey": s.GetSessionKey(), "stream": "lifecycle",
		"data": map[string]any{"phase": "error", "error": f.Error.Message, "endedAt": nowUnixMs()},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
}

// clearTurn resets correlation on disconnect or busy-TTL expiry.
func (s *CodexService) clearTurn() {
	telemetry.ReportTaskObservationLost(s.unfinishedTaskRunIDs()...)
	s.currentRequestID.Store("")
	s.currentRunID.Store("")
	s.pendingMu.Lock()
	s.pendingRuns = nil
	s.steeredRuns = nil
	s.pendingMu.Unlock()
	s.turnMu.Lock()
	s.assistantParts = nil
	s.toolStartSeen = nil
	s.turnMu.Unlock()
}

// markPendingRunSent records transport evidence without changing admission or correlation.
func (s *CodexService) markPendingRunSent(reqID string) {
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
func (s *CodexService) unfinishedTaskRunIDs() []string {
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
	for _, pending := range s.steeredRuns {
		ids = append(ids, pending.runID)
	}
	return ids
}
