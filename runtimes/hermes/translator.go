package hermes

import (
	"encoding/json"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// nowUnixMs returns the current time in milliseconds (matches the OpenClaw frame timestamp convention).
func nowUnixMs() int64 { return time.Now().UnixMilli() }

// emitRunID returns the runId the translator stamps on every WSEvent.
func (s *HermesService) emitRunID(result *streamResult, respID string) string {
	if result != nil && result.DeviceRunID != "" {
		return result.DeviceRunID
	}
	if respID != "" {
		return respID
	}
	id, _ := s.lastResponseID.Load().(string)
	return id
}

// hermesUsage decodes the Hermes /v1/responses usage block (snake_case) so we can rewrap it into domain.TokenUsage (camelCase) without exporting Hermes- specific types.
type hermesUsage struct {
	InputTokens  int `json:"input_tokens"`
	OutputTokens int `json:"output_tokens"`
	TotalTokens  int `json:"total_tokens"`
	// OpenAI usage: input_tokens INCLUDES cache reads/writes; TokenUsage stores uncached input separately.
	InputTokensDetails struct {
		CachedTokens     int `json:"cached_tokens"`
		CacheWriteTokens int `json:"cache_write_tokens"`
	} `json:"input_tokens_details"`
}

func (u *hermesUsage) toDomain() *domain.TokenUsage {
	if u == nil {
		return nil
	}
	if u.InputTokens == 0 && u.OutputTokens == 0 && u.TotalTokens == 0 {
		return nil
	}
	in := u.InputTokens
	cached := max(0, u.InputTokensDetails.CachedTokens)
	written := max(0, u.InputTokensDetails.CacheWriteTokens)
	if cached > in || written > in-cached {
		cached, written = 0, 0
	}
	in -= cached + written
	return &domain.TokenUsage{
		InputTokens:      in,
		OutputTokens:     u.OutputTokens,
		TotalTokens:      u.TotalTokens,
		CacheReadTokens:  cached,
		CacheWriteTokens: written,
	}
}

// translateSSE parses one (event, data) pair from the Hermes SSE stream and emits 0..N domain.WSEvent frames into dispatch.
func (s *HermesService) translateSSE(eventName, data string, dispatch func(domain.WSEvent), result *streamResult) {
	var probe map[string]json.RawMessage
	if err := json.Unmarshal([]byte(data), &probe); err != nil {
		slog.Debug("hermes SSE: non-JSON data, ignored", "component", "hermes", "data", truncRunes(data, 200))
		return
	}

	kind := eventName
	if kind == "" {
		if t, ok := probe["type"]; ok {
			_ = json.Unmarshal(t, &kind)
		}
	}

	switch kind {
	case "response.created":
		s.handleResponseCreated(probe, dispatch, result)
	case "response.output_item.added":
		s.handleOutputItemAdded(probe, dispatch, result)
	case "response.output_item.done":
		s.handleOutputItemDone(probe, dispatch)
	case "response.output_text.delta":
		s.handleOutputTextDelta(probe, dispatch, result)
	case "response.output_text.done":
	case "response.completed":
		s.handleResponseCompleted(probe, dispatch, result)
	case "response.failed":
		s.handleResponseFailed(probe, dispatch, result)
	default:
		slog.Debug("hermes SSE: unhandled event", "component", "hermes", "event", kind)
	}
}

// handleResponseCreated extracts the response.id and session UUID (carried in the response object), stores them, and emits lifecycle.start.
func (s *HermesService) handleResponseCreated(probe map[string]json.RawMessage, dispatch func(domain.WSEvent), result *streamResult) {
	var inner struct {
		Response struct {
			ID           string `json:"id"`
			Model        string `json:"model"`
			Conversation struct {
				ID string `json:"id"`
			} `json:"conversation"`
			SessionID string `json:"session_id"`
		} `json:"response"`
	}
	_ = jsonRemarshal(probe, &inner)

	respID := inner.Response.ID
	if respID != "" {
		s.lastResponseID.Store(respID)
		result.ResponseID = respID
	}
	if inner.Response.SessionID != "" {
		s.sessionUUID.Store(inner.Response.SessionID)
		result.SessionID = inner.Response.SessionID
	}

	slog.Info("hermes <<< SSE response.created (turn started)", "component", "hermes",
		"responseID", respID,
		"runId", s.emitRunID(result, respID),
		"sessionID", s.GetSessionKey(),
		"model", inner.Response.Model)

	payload, _ := json.Marshal(map[string]any{
		"runId":      s.emitRunID(result, respID),
		"sessionKey": s.GetSessionKey(),
		"stream":     "lifecycle",
		"data": map[string]any{
			"phase":     "start",
			"startedAt": nowUnixMs(),
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
}

// handleOutputItemAdded maps function_call to tool.start and function_call_output to tool.end.
func (s *HermesService) handleOutputItemAdded(probe map[string]json.RawMessage, dispatch func(domain.WSEvent), result *streamResult) {
	var inner struct {
		OutputIndex int `json:"output_index"`
		Item        struct {
			Type      string          `json:"type"`
			ID        string          `json:"id"`
			Name      string          `json:"name"`
			CallID    string          `json:"call_id"`
			Arguments string          `json:"arguments"`
			Output    json.RawMessage `json:"output"`
		} `json:"item"`
	}
	if err := jsonRemarshal(probe, &inner); err != nil {
		return
	}

	runID := s.emitRunID(result, "")

	switch inner.Item.Type {
	case "function_call":
		slog.Info("hermes <<< SSE tool CALL", "component", "hermes",
			"runID", runID,
			"tool", inner.Item.Name,
			"toolCallId", inner.Item.CallID,
			"argsLen", len(inner.Item.Arguments),
			"args", truncRunes(inner.Item.Arguments, 400))
		payload, _ := json.Marshal(map[string]any{
			"runId":      runID,
			"sessionKey": s.GetSessionKey(),
			"stream":     "tool",
			"data": map[string]any{
				"phase":      "start",
				"name":       inner.Item.Name,
				"toolCallId": inner.Item.CallID,
				"arguments":  inner.Item.Arguments,
			},
		})
		dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})

	case "function_call_output":
		result := inner.Item.Output
		if len(result) == 0 {
			result = json.RawMessage(`""`)
		}
		slog.Info("hermes <<< SSE tool RESULT", "component", "hermes",
			"runID", runID,
			"toolCallId", inner.Item.CallID,
			"resultLen", len(result),
			"result", truncRunes(string(result), 400))
		payload, _ := json.Marshal(map[string]any{
			"runId":      runID,
			"sessionKey": s.GetSessionKey(),
			"stream":     "tool",
			"data": map[string]any{
				"phase":      "end",
				"toolCallId": inner.Item.CallID,
				"result":     json.RawMessage(result),
			},
		})
		dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})

	case "message":
		slog.Debug("hermes <<< SSE assistant message item opened (waiting for deltas)", "component", "hermes",
			"runID", runID, "itemId", inner.Item.ID)
	}
}

// handleOutputItemDone is largely a parity hook.
func (s *HermesService) handleOutputItemDone(_ map[string]json.RawMessage, _ func(domain.WSEvent)) {
}

// handleOutputTextDelta streams assistant deltas.
func (s *HermesService) handleOutputTextDelta(probe map[string]json.RawMessage, dispatch func(domain.WSEvent), result *streamResult) {
	var inner struct {
		Delta string `json:"delta"`
	}
	if err := jsonRemarshal(probe, &inner); err != nil || inner.Delta == "" {
		return
	}
	runID := s.emitRunID(result, "")
	slog.Debug("hermes <<< SSE delta", "component", "hermes",
		"runID", runID, "delta", inner.Delta)
	payload, _ := json.Marshal(map[string]any{
		"runId":      runID,
		"sessionKey": s.GetSessionKey(),
		"stream":     "assistant",
		"data": map[string]any{
			"delta": inner.Delta,
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: payload})
}

// handleResponseCompleted emits (a) the final chat message and (b) the lifecycle.end with usage.
func (s *HermesService) handleResponseCompleted(probe map[string]json.RawMessage, dispatch func(domain.WSEvent), result *streamResult) {
	result.Terminal = true
	var inner struct {
		Response struct {
			ID     string `json:"id"`
			Output []struct {
				Type    string `json:"type"`
				Role    string `json:"role"`
				Content []struct {
					Type string `json:"type"`
					Text string `json:"text"`
				} `json:"content"`
			} `json:"output"`
			Usage *hermesUsage `json:"usage,omitempty"`
		} `json:"response"`
	}
	_ = jsonRemarshal(probe, &inner)

	respID := inner.Response.ID
	if respID == "" {
		respID, _ = s.lastResponseID.Load().(string)
	}
	result.ResponseID = respID
	emitID := s.emitRunID(result, respID)

	// Collect every message content[].text part as the authoritative final text.
	var b strings.Builder
	for _, item := range inner.Response.Output {
		if item.Type != "message" {
			continue
		}
		for _, c := range item.Content {
			if c.Type == "output_text" || c.Type == "text" {
				b.WriteString(c.Text)
			}
		}
	}
	finalText := b.String()
	result.FinalText = finalText

	usage := inner.Response.Usage
	logArgs := []any{
		"component", "hermes",
		"runID", respID,
		"emitRunID", emitID,
		"finalLen", len(finalText),
		"final", truncRunes(finalText, 500),
	}
	if usage != nil {
		logArgs = append(logArgs,
			"inputTokens", usage.InputTokens,
			"outputTokens", usage.OutputTokens,
			"totalTokens", usage.TotalTokens,
			"cacheReadTokens", usage.InputTokensDetails.CachedTokens,
			"cacheWriteTokens", usage.InputTokensDetails.CacheWriteTokens)
	}
	slog.Info("hermes <<< SSE response.completed (assistant final)", logArgs...)

	chatMsg, _ := json.Marshal(map[string]any{
		"runId":      emitID,
		"sessionKey": s.GetSessionKey(),
		"state":      "final",
		"role":       "assistant",
		"message":    finalText,
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "chat", Payload: chatMsg})

	endPayload, _ := json.Marshal(map[string]any{
		"runId":      emitID,
		"sessionKey": s.GetSessionKey(),
		"stream":     "lifecycle",
		"data": map[string]any{
			"phase":   "end",
			"endedAt": nowUnixMs(),
			"usage":   inner.Response.Usage.toDomain(),
		},
	})
	dispatch(domain.WSEvent{Type: "evt", Event: "agent", Payload: endPayload})
}

func (s *HermesService) handleResponseFailed(probe map[string]json.RawMessage, dispatch func(domain.WSEvent), result *streamResult) {
	var inner struct {
		Response struct {
			ID    string `json:"id"`
			Error struct {
				Message string `json:"message"`
				Type    string `json:"type"`
			} `json:"error"`
		} `json:"response"`
		Error struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	_ = jsonRemarshal(probe, &inner)

	msg := inner.Response.Error.Message
	if msg == "" {
		msg = inner.Error.Message
	}
	if msg == "" {
		msg = "hermes response failed"
	}
	result.Terminal = true
	result.Errored = true
	result.ErrorText = msg

	respID := inner.Response.ID
	if respID == "" {
		respID, _ = s.lastResponseID.Load().(string)
	}
	emitID := s.emitRunID(result, respID)

	slog.Warn("hermes <<< SSE response.failed", "component", "hermes",
		"runID", respID, "emitRunID", emitID, "error", msg)
	payload, _ := json.Marshal(map[string]any{
		"runId":      emitID,
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

// jsonRemarshal turns the lazy-decoded map back into the typed struct.
func jsonRemarshal(src map[string]json.RawMessage, dst any) error {
	raw, err := json.Marshal(src)
	if err != nil {
		return err
	}
	return json.Unmarshal(raw, dst)
}
