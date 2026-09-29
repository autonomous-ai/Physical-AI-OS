package http

import (
	"encoding/json"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
)

// Incomplete-turn recovery: OpenClaw can report "couldn't generate a response" even when the
// model replied (openclaw#68076, #67855, #98528); the reply is salvaged on lifecycle:error.

// errorRecoveryTTL is how long a recovered run suppresses follow-up error banners; covers
// the gateway's auto-retry error (~15s later) without masking a later turn's failure.
const errorRecoveryTTL = 2 * time.Minute

// tryRecoverIncompleteTurn salvages an errored run's reply from buffered deltas, else from
// chat.history after the last user message. Returns true when a reply was recovered and emitted.
func (h *AgentHandler) tryRecoverIncompleteTurn(runID, flowRunID, sessionKey string) bool {
	// Device-originated runs and the main session only; sub-sessions have no device-facing reply.
	if !isDeviceOutboundChatRunID(flowRunID) && sessionKey != h.agentGateway.GetSessionKey() {
		return false
	}

	// Persist streaming counters like lifecycle:end, or Flow Monitor shows no assistant row.
	if s := h.drainStreamStats(flowRunID); s != nil && s.assistantChunks > 0 {
		flow.Log("agent_last_token", map[string]any{
			"run_id": flowRunID,
			"text":   s.assistantText.String(),
			"chunks": s.assistantChunks,
			"chars":  s.assistantChars,
		}, flowRunID)
	}

	text, hwCalls := h.flushAssistantText(runID)
	source := "stream_buffer"
	if strings.TrimSpace(text) == "" {
		hist, err := h.agentGateway.FetchChatHistory(sessionKey, 10)
		if err != nil || hist == nil {
			return false
		}
		raw := extractTrailingAssistantFromHistory(hist)
		if strings.TrimSpace(raw) == "" {
			return false
		}
		// Strip but never fire history markers: their side effects already ran in the gateway.
		_, text = extractHWCalls(raw)
		hwCalls = nil
		source = "chat_history"
	}

	// Sentinels mean nothing user-visible was lost — let the error stand.
	if strings.Contains(strings.ToUpper(text), "HEARTBEAT_OK") {
		return false
	}
	text = extractSayTag(text)
	text = sanitizeAgentText(text)
	if strings.TrimSpace(text) == "" || isAgentNoReply(text) {
		return false
	}

	f := newCoTLeakFilter(h.replyLanguageCode())
	filtered := strings.TrimSpace(f.filterText(text))
	if len(f.dropped) > 0 {
		flow.Log("cot_leak_filtered", map[string]any{
			"run_id":  flowRunID,
			"dropped": len(f.dropped),
			"preview": cotDroppedPreview(f.dropped, 500),
		}, flowRunID)
	}
	if filtered == "" {
		return false
	}
	text = filtered

	// Fire only markers not already fired at stream time (tryFirstSentenceFlush).
	fired := h.consumeFiredHWCount(runID)
	if fired > len(hwCalls) {
		fired = len(hwCalls)
	}
	if rest := hwCalls[fired:]; len(rest) > 0 {
		h.fireHWCallsSync(rest, flowRunID)
	}

	// Consuming the suppress flags here is correct: no lifecycle:end follows for this run.
	suppress := h.clearTTSSuppress(runID)
	if suppress == "" && h.agentGateway.ConsumeWebChatRun(flowRunID) {
		suppress = "web_chat"
	}
	if suppress == "" && h.agentGateway.ConsumeSilentRun(flowRunID) {
		suppress = "voice_agent_handled"
	}
	if suppress == "" && isChannelOriginatedRun(runID, flowRunID) {
		suppress = "channel_run"
	}

	streamedLen := h.consumeStreamedCleanLen(runID)
	if streamedLen > len(text) {
		streamedLen = len(text)
	}
	remainder := strings.TrimSpace(text[streamedLen:])

	slog.Warn("recovered assistant reply from errored turn",
		"component", "agent", "run_id", flowRunID, "source", source,
		"chars", len(text), "suppress", suppress)
	flow.Log("agent_error_recovered", map[string]any{
		"run_id": flowRunID,
		"source": source,
		"chars":  len(text),
	}, flowRunID)
	h.monitorBus.Push(domain.MonitorEvent{
		Type:    "chat_response",
		Summary: text[:min(len(text), 120)],
		RunID:   flowRunID,
		State:   "final",
		Detail:  map[string]string{"role": "assistant", "message": text, "recovered": "true"},
	})

	switch {
	case suppress != "":
		flow.Log("tts_suppressed", map[string]any{"run_id": flowRunID, "reason": suppress, "text": text}, flowRunID)
	case remainder != "":
		// full_text carries the whole reply for web display; remainder skips the already-streamed first sentence.
		flow.Log("tts_send", map[string]any{"run_id": flowRunID, "text": remainder, "full_text": text, "streamed_len": streamedLen}, flowRunID)
		h.deliverTTSQueue(remainder, flowRunID, "recovered-reply TTS delivery failed")
	default:
		// Single-sentence reply already fully streamed mid-turn.
		flow.Log("tts_stream_complete", map[string]any{"run_id": flowRunID, "text": text}, flowRunID)
	}
	return true
}

// extractTrailingAssistantFromHistory returns assistant text after the last user message in a
// chat.history payload, or "" when there is no user anchor (guards against replaying the previous reply).
func extractTrailingAssistantFromHistory(payload json.RawMessage) string {
	var hist struct {
		Messages []struct {
			Role    string          `json:"role"`
			Content json.RawMessage `json:"content"`
		} `json:"messages"`
	}
	if json.Unmarshal(payload, &hist) != nil {
		return ""
	}
	lastUser := -1
	for i, m := range hist.Messages {
		if m.Role == "user" {
			lastUser = i
		}
	}
	if lastUser < 0 {
		return ""
	}
	var parts []string
	for _, m := range hist.Messages[lastUser+1:] {
		if m.Role != "assistant" {
			continue
		}
		if t := historyContentText(m.Content); strings.TrimSpace(t) != "" {
			parts = append(parts, t)
		}
	}
	return strings.TrimSpace(strings.Join(parts, "\n"))
}

// historyContentText flattens chat.history content (string or [{type,text}] blocks).
func historyContentText(content json.RawMessage) string {
	var s string
	if json.Unmarshal(content, &s) == nil {
		return s
	}
	var blocks []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if json.Unmarshal(content, &blocks) == nil {
		var parts []string
		for _, b := range blocks {
			if b.Type == "text" && strings.TrimSpace(b.Text) != "" {
				parts = append(parts, b.Text)
			}
		}
		return strings.Join(parts, " ")
	}
	return ""
}

// markErrorRecovered records that flowRunID's reply was recovered so later chat-stream errors are suppressed.
func (h *AgentHandler) markErrorRecovered(flowRunID string) {
	now := time.Now()
	h.errorRecoveredMu.Lock()
	defer h.errorRecoveredMu.Unlock()
	for id, t := range h.errorRecoveredRuns {
		if now.Sub(t) > errorRecoveryTTL {
			delete(h.errorRecoveredRuns, id)
		}
	}
	h.errorRecoveredRuns[flowRunID] = now
}

// wasErrorRecovered reports whether flowRunID had its reply recovered within errorRecoveryTTL.
func (h *AgentHandler) wasErrorRecovered(flowRunID string) bool {
	h.errorRecoveredMu.Lock()
	defer h.errorRecoveredMu.Unlock()
	t, ok := h.errorRecoveredRuns[flowRunID]
	return ok && time.Since(t) <= errorRecoveryTTL
}
