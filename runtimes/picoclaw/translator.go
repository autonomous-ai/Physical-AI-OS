package picoclaw

import (
	"encoding/json"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// nowUnixMs returns the current time in milliseconds (matches the OpenClaw frame
// timestamp convention).
func nowUnixMs() int64 { return time.Now().UnixMilli() }

// picoFrame is one inbound PicoClaw message.
type picoFrame struct {
	Type      string      `json:"type"`
	SessionID string      `json:"session_id"`
	Timestamp int64       `json:"timestamp"`
	Payload   picoPayload `json:"payload"`
}

type picoPayload struct {
	Content     string         `json:"content"`
	MessageID   string         `json:"message_id"`
	Placeholder bool           `json:"placeholder"`
	Kind        string         `json:"kind"`
	Thought     bool           `json:"thought"` // legacy reasoning flag
	ModelName   string         `json:"model_name"`
	ToolCalls   []picoToolCall `json:"tool_calls"`
	Usage       *picoUsage     `json:"context_usage"`

	// error frames
	Code    string `json:"code"`
	Message string `json:"message"`
}

type picoToolCall struct {
	ID       string `json:"id"`
	Type     string `json:"type"`
	Function struct {
		Name      string `json:"name"`
		Arguments string `json:"arguments"` // JSON string, not an object
	} `json:"function"`
}

// picoUsage is PicoClaw's context_usage block (only present on the final frame).
type picoUsage struct {
	UsedTokens        int `json:"used_tokens"`
	TotalTokens       int `json:"total_tokens"`
	HistoryTokens     int `json:"history_tokens"`
	CompressAtTokens  int `json:"compress_at_tokens"`
	SummarizeAtTokens int `json:"summarize_at_tokens"`
	UsedPercent       int `json:"used_percent"`
}

func (u *picoUsage) toDomain() *domain.TokenUsage {
	if u == nil {
		return nil
	}
	if u.UsedTokens == 0 && u.TotalTokens == 0 && u.HistoryTokens == 0 {
		return nil
	}
	return &domain.TokenUsage{
		InputTokens:       u.HistoryTokens,
		TotalTokens:       u.UsedTokens,
		CompressAtTokens:  u.CompressAtTokens,
		SummarizeAtTokens: u.SummarizeAtTokens,
	}
}

// category classifies a message.create / message.update payload.
type category int

const (
	catOther    category = iota // empty / not a renderable message
	catThinking                 // placeholder ("Thinking...") or reasoning (kind=thought)
	catTool                     // tool_calls
	catFinal                    // real final answer (ends the turn)
)

func categorize(p picoPayload) category {
	if p.Placeholder {
		return catThinking
	}
	if strings.EqualFold(strings.TrimSpace(p.Kind), "thought") || p.Thought {
		return catThinking
	}
	if strings.EqualFold(strings.TrimSpace(p.Kind), "tool_calls") || len(p.ToolCalls) > 0 {
		return catTool
	}
	if strings.TrimSpace(p.Content) != "" {
		return catFinal
	}
	return catOther
}

// translateFrame parses one inbound PicoClaw frame and emits 0..N domain.WSEvent
// frames into dispatch.
func (s *PicoclawService) translateFrame(raw []byte, dispatch func(domain.WSEvent)) {
	var f picoFrame
	if err := json.Unmarshal(raw, &f); err != nil {
		slog.Debug("picoclaw: non-JSON frame, ignored", "component", "picoclaw", "raw", truncRunes(string(raw), 200))
		return
	}

	if f.SessionID != "" && f.SessionID != s.GetSessionKey() {
		s.SetSessionKey(f.SessionID)
	}

	switch f.Type {
	case "typing.start":
		s.ensureTurnStarted(dispatch)
	case "typing.stop", "message.delete", "pong":
		// typing.stop arrives BEFORE the final answer (right after the thinking
		// phase) — it is NOT the end of the turn.
		// message.delete removes the "Thinking..." placeholder we never rendered.
	case "error":
		s.handleError(f, dispatch)
	case "message.create", "message.update":
		s.handleMessage(f, dispatch)
	default:
		slog.Debug("picoclaw: unhandled frame type", "component", "picoclaw", "type", f.Type)
	}
}

func (s *PicoclawService) handleMessage(f picoFrame, dispatch func(domain.WSEvent)) {
	switch categorize(f.Payload) {
	case catThinking:
		s.ensureTurnStarted(dispatch)
	case catTool:
		s.ensureTurnStarted(dispatch)
		s.emitToolCalls(f, dispatch)
	case catFinal:
		s.ensureTurnStarted(dispatch)
		s.emitFinal(f, dispatch)
	case catOther:
	}
}

// ensureTurnStarted emits lifecycle.start exactly once per turn.
func (s *PicoclawService) ensureTurnStarted(dispatch func(domain.WSEvent)) {
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

	slog.Info("picoclaw <<< turn started", "component", "picoclaw", "runID", runID, "sessionKey", s.GetSessionKey())
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

// emitToolCalls surfaces each OpenAI-style tool call as a tool.start + tool.end
// pair.
func (s *PicoclawService) emitToolCalls(f picoFrame, dispatch func(domain.WSEvent)) {
	runID := s.getCurrentRunID()
	for _, c := range f.Payload.ToolCalls {
		name := c.Function.Name
		if name == "" {
			name = "tool"
		}
		args := c.Function.Arguments
		callID := c.ID
		slog.Info("picoclaw <<< tool CALL", "component", "picoclaw",
			"runID", runID, "tool", name, "toolCallId", callID, "argsLen", len(args))

		startPayload, _ := json.Marshal(map[string]any{
			"runId":      runID,
			"sessionKey": s.GetSessionKey(),
			"stream":     "tool",
			"data": map[string]any{
				"phase":      "start",
				"name":       name,
				"toolCallId": callID,
				"arguments":  args,
			},
		})
		dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: startPayload})

		endPayload, _ := json.Marshal(map[string]any{
			"runId":      runID,
			"sessionKey": s.GetSessionKey(),
			"stream":     "tool",
			"data": map[string]any{
				"phase":      "end",
				"toolCallId": callID,
				"result":     "",
			},
		})
		dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: endPayload})
	}
}

// emitFinal emits, in order: (a) the whole reply as a single assistant delta,
// (b) the final chat message, (c) lifecycle.end with usage — then closes the
// turn.
func (s *PicoclawService) emitFinal(f picoFrame, dispatch func(domain.WSEvent)) {
	runID := s.getCurrentRunID()
	finalText := f.Payload.Content

	logArgs := []any{
		"component", "picoclaw",
		"runID", runID,
		"finalLen", len(finalText),
		"final", truncRunes(finalText, 500),
	}
	if f.Payload.Usage != nil {
		logArgs = append(logArgs,
			"usedTokens", f.Payload.Usage.UsedTokens,
			"compressAt", f.Payload.Usage.CompressAtTokens,
			"summarizeAt", f.Payload.Usage.SummarizeAtTokens,
			"usedPercent", f.Payload.Usage.UsedPercent)
		s.lastCompressAt.Store(int64(f.Payload.Usage.CompressAtTokens))
	}
	slog.Info("picoclaw <<< final answer", logArgs...)

	defer s.finishTurn()

	// Surface the full reply as a single assistant delta BEFORE chat.final /
	// lifecycle.end so the consumer's assistant buffer (accumulateAssistantDelta)
	// is populated before it flushes at lifecycle.end — see this func's doc.
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
			"usage":   f.Payload.Usage.toDomain(),
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: endPayload})
}

func (s *PicoclawService) handleError(f picoFrame, dispatch func(domain.WSEvent)) {
	s.ensureTurnStarted(dispatch)
	runID := s.getCurrentRunID()
	msg := f.Payload.Message
	if msg == "" {
		msg = f.Payload.Code
	}
	if msg == "" {
		msg = "picoclaw error"
	}
	slog.Warn("picoclaw <<< error", "component", "picoclaw", "runID", runID, "code", f.Payload.Code, "error", msg)

	defer s.finishTurn()

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

// finishTurn releases admission only after all terminal callbacks have run.
func (s *PicoclawService) finishTurn() {
	s.sendMu.Lock()
	s.RemovePendingChatTraceByRunID(s.getCurrentRunID())
	s.clearTurn()
	s.activeTurn.Store(false)
	s.sendMu.Unlock()
	s.drainPendingEvents()
}

func (s *PicoclawService) peekPendingRunID() string {
	v, _ := s.pendingRunID.Load().(string)
	return v
}

func (s *PicoclawService) getCurrentRunID() string {
	v, _ := s.currentRunID.Load().(string)
	return v
}

func (s *PicoclawService) setCurrentRunID(runID string) { s.currentRunID.Store(runID) }

func (s *PicoclawService) setPendingRunID(runID string) { s.pendingRunID.Store(runID) }

func (s *PicoclawService) consumePendingRunID() string {
	v, _ := s.pendingRunID.Load().(string)
	if v != "" {
		s.pendingRunID.Store("")
	}
	return v
}

// clearTurn resets the in-flight turn ids without touching busy state.
func (s *PicoclawService) clearTurn() {
	s.currentRunID.Store("")
	s.pendingRunID.Store("")
}
