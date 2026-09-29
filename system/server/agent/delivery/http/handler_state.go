package http

import (
	"log/slog"
	"strings"
	"time"
)

// accumulateAssistantDelta appends a delta to the buffer for the given runId.
func (h *AgentHandler) accumulateAssistantDelta(runID, delta string) {
	if delta == "" {
		return
	}
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()
	buf, ok := h.assistantBuf[runID]
	if !ok {
		buf = &strings.Builder{}
		h.assistantBuf[runID] = buf
	}
	buf.WriteString(delta)
	slog.Info("assistant delta buffered (first-sentence streaming checked separately; remainder at lifecycle:end)",
		"component", "agent",
		"run_id", runID,
		"delta", delta,
		"cumulative_len", buf.Len(),
		"cumulative_tail", tailPreview(buf.String(), 120),
	)
}

// tailPreview returns the last n bytes of s for logging.
func tailPreview(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return "…" + s[len(s)-n:]
}

// tryFirstSentenceFlush returns the first complete sentence safe to stream to TTS,
// or "" if none is ready or one was already streamed. Only the first sentence is
// streamed; the rest goes via speak-queue at lifecycle:end. The raw buffer is kept.
// Defers on partial HW markers, <say> wrappers and NO_REPLY/HEARTBEAT_OK (streamed
// text cannot be unspoken).
func (h *AgentHandler) tryFirstSentenceFlush(runID string) string {
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()

	if _, already := h.streamedCleanLen[runID]; already {
		return ""
	}
	buf, ok := h.assistantBuf[runID]
	if !ok || buf.Len() == 0 {
		return ""
	}
	raw := buf.String()
	if hasPartialHWMarker(raw) || hasPartialHWLinkMarker(raw) {
		return ""
	}
	if strings.Contains(raw, "<say>") {
		return ""
	}
	upper := strings.ToUpper(raw)
	if strings.Contains(upper, "NO_REPLY") || strings.Contains(upper, "HEARTBEAT_OK") {
		return ""
	}

	_, cleaned := extractHWCalls(raw)
	cleaned = prunedImageMarkerRe.ReplaceAllString(cleaned, "")
	cleaned = strings.TrimSpace(cleaned)
	if cleaned == "" {
		return ""
	}

	boundary := findSentenceFlushBoundary(cleaned)
	if boundary < 0 {
		return ""
	}
	sentence := strings.TrimSpace(cleaned[:boundary+1])
	if sentence == "" {
		return ""
	}
	// Drop leaked CoT; if all of it is CoT, defer without marking streamed.
	f := newCoTLeakFilter(h.replyLanguageCode())
	filtered := strings.TrimSpace(f.filterText(sentence))
	if len(f.dropped) > 0 {
		slog.Warn("CoT leak dropped from first-sentence stream (not spoken)",
			"component", "agent", "run_id", runID,
			"dropped", len(f.dropped),
			"preview", cotDroppedPreview(f.dropped, 200))
	}
	if filtered == "" {
		return ""
	}
	// Never speak silence narration; defer without marking streamed.
	if isMetaNonReply(filtered) {
		return ""
	}
	h.streamedCleanLen[runID] = boundary + 1
	return filtered
}

// replyLanguageCode returns the configured device language code (e.g. "vi", "en").
func (h *AgentHandler) replyLanguageCode() string {
	return h.config.STTLanguage
}

// cleanedSlackStreamText returns the whole cleaned reply for Slack streaming, with
// false when it must defer (same defer rules as tryFirstSentenceFlush).
func (h *AgentHandler) cleanedSlackStreamText(runID string) (string, bool) {
	h.assistantMu.Lock()
	buf, ok := h.assistantBuf[runID]
	var raw string
	if ok && buf != nil {
		raw = buf.String()
	}
	h.assistantMu.Unlock()
	if raw == "" {
		return "", false
	}
	if hasPartialHWMarker(raw) || hasPartialHWLinkMarker(raw) || strings.Contains(raw, "<say>") {
		return "", false
	}
	upper := strings.ToUpper(raw)
	if strings.Contains(upper, "NO_REPLY") || strings.Contains(upper, "HEARTBEAT_OK") {
		return "", false
	}
	_, cleaned := extractHWCalls(raw)
	cleaned = prunedImageMarkerRe.ReplaceAllString(cleaned, "")
	cleaned = strings.TrimSpace(cleaned)
	if cleaned == "" {
		return "", false
	}
	return cleaned, true
}

// consumeStreamedCleanLen returns and clears the cleaned-reply byte offset already
// streamed to TTS for runID (0 if none).
func (h *AgentHandler) consumeStreamedCleanLen(runID string) int {
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()
	n, ok := h.streamedCleanLen[runID]
	if !ok {
		return 0
	}
	delete(h.streamedCleanLen, runID)
	return n
}

// readFiredHWCount returns the stream-time fired HW marker count for runID without clearing it.
func (h *AgentHandler) readFiredHWCount(runID string) int {
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()
	return h.firedHWCount[runID]
}

// recordFiredHWCount sets the stream-time fired HW marker count for runID.
func (h *AgentHandler) recordFiredHWCount(runID string, count int) {
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()
	h.firedHWCount[runID] = count
}

// consumeFiredHWCount returns and clears the fired HW marker count for runID.
func (h *AgentHandler) consumeFiredHWCount(runID string) int {
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()
	n := h.firedHWCount[runID]
	delete(h.firedHWCount, runID)
	return n
}

// hasPartialHWMarker reports whether text has a `[HW:` opener with no closing `]` yet.
func hasPartialHWMarker(text string) bool {
	idx := strings.Index(text, "[HW:")
	for idx >= 0 {
		end := strings.Index(text[idx:], "]")
		if end < 0 {
			return true
		}
		next := strings.Index(text[idx+4:], "[HW:")
		if next < 0 {
			return false
		}
		idx = idx + 4 + next
	}
	return false
}

// hasPartialHWLinkMarker reports whether text has an unclosed link-form `](HW:` marker
// (case-insensitive) or ends mid-signature. Example: `[Lights off](HW:/led/of` → true.
func hasPartialHWLinkMarker(text string) bool {
	lower := strings.ToLower(text)
	idx := strings.Index(lower, "](hw:")
	for idx >= 0 {
		rest := lower[idx:]
		if !strings.Contains(rest, ")") {
			return true
		}
		next := strings.Index(lower[idx+5:], "](hw:")
		if next < 0 {
			break
		}
		idx = idx + 5 + next
	}
	// Bare trailing `]` is not deferred on: every complete canonical marker ends with it.
	for _, suf := range []string{"](", "](h", "](hw"} {
		if strings.HasSuffix(lower, suf) {
			return true
		}
	}
	return false
}

// findSentenceFlushBoundary returns the rightmost index of `[.?!]` followed by
// whitespace, or -1. Skips decimal-like "5. 5".
func findSentenceFlushBoundary(s string) int {
	n := len(s)
	for i := n - 2; i >= 0; i-- {
		c := s[i]
		if c != '.' && c != '?' && c != '!' {
			continue
		}
		next := s[i+1]
		if next != ' ' && next != '\n' && next != '\t' && next != '\r' {
			continue
		}
		if i > 0 && isAsciiDigit(s[i-1]) {
			j := i + 1
			for j < n && (s[j] == ' ' || s[j] == '\t') {
				j++
			}
			if j < n && isAsciiDigit(s[j]) {
				continue
			}
		}
		return i
	}
	return -1
}

func isAsciiDigit(b byte) bool {
	return b >= '0' && b <= '9'
}

// demoteAssistantBufferToThinking removes and returns pre-tool-call text (plan
// narration, not the reply). Keeps the buffer and returns "" if the first sentence
// was already streamed or the text carries an HW marker that must still fire.
func (h *AgentHandler) demoteAssistantBufferToThinking(runID string) string {
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()
	buf, ok := h.assistantBuf[runID]
	if !ok || buf.Len() == 0 {
		return ""
	}
	if _, streamed := h.streamedCleanLen[runID]; streamed {
		return ""
	}
	raw := buf.String()
	if hasPartialHWMarker(raw) || hasPartialHWLinkMarker(raw) {
		return ""
	}
	if calls, _ := extractHWCalls(raw); len(calls) > 0 {
		return ""
	}
	delete(h.assistantBuf, runID)
	return strings.TrimSpace(raw)
}

// flushAssistantText returns and clears the buffered text for runID with HW markers
// stripped, plus the extracted HW calls.
func (h *AgentHandler) flushAssistantText(runID string) (string, []hwCall) {
	h.assistantMu.Lock()
	defer h.assistantMu.Unlock()
	buf, ok := h.assistantBuf[runID]
	if !ok || buf.Len() == 0 {
		return "", nil
	}
	raw := buf.String()
	raw = prunedImageMarkerRe.ReplaceAllString(raw, "")
	calls, text := extractHWCalls(raw)
	text = strings.TrimSpace(text)
	delete(h.assistantBuf, runID)
	return text, calls
}

// recordAssistantDelta updates streaming counters and reports whether this is the run's first delta.
func (h *AgentHandler) recordAssistantDelta(runID, delta string) (isFirst bool) {
	if delta == "" {
		return false
	}
	h.streamStatsMu.Lock()
	defer h.streamStatsMu.Unlock()
	s, ok := h.streamStats[runID]
	if !ok {
		s = &runStreamStats{}
		h.streamStats[runID] = s
	}
	isFirst = !s.assistantFirstSeen
	if isFirst {
		s.assistantFirstAt = time.Now()
	}
	s.assistantFirstSeen = true
	s.assistantChunks++
	s.assistantChars += len(delta)
	s.assistantText.WriteString(delta)
	return isFirst
}

// recordThinkingDelta is the thinking-stream counterpart of recordAssistantDelta.
func (h *AgentHandler) recordThinkingDelta(runID, delta string) (isFirst bool) {
	if delta == "" {
		return false
	}
	h.streamStatsMu.Lock()
	defer h.streamStatsMu.Unlock()
	s, ok := h.streamStats[runID]
	if !ok {
		s = &runStreamStats{}
		h.streamStats[runID] = s
	}
	isFirst = !s.thinkingFirstSeen
	s.thinkingFirstSeen = true
	s.thinkingChunks++
	s.thinkingChars += len(delta)
	s.thinkingText.WriteString(delta)
	return isFirst
}

// drainStreamStats returns and clears the stats for runID (nil if none).
func (h *AgentHandler) drainStreamStats(runID string) *runStreamStats {
	h.streamStatsMu.Lock()
	defer h.streamStatsMu.Unlock()
	s, ok := h.streamStats[runID]
	if !ok {
		return nil
	}
	delete(h.streamStats, runID)
	return s
}

// suppressTTS flags a runID to skip TTS on lifecycle end with the given reason.
func (h *AgentHandler) suppressTTS(runID, reason string) {
	h.ttsSuppressMu.Lock()
	defer h.ttsSuppressMu.Unlock()
	// "music_playing" takes priority over "already_spoken".
	if existing := h.ttsSuppressReasons[runID]; existing == "music_playing" && reason != "music_playing" {
		return
	}
	h.ttsSuppressReasons[runID] = reason
}

// clearTTSSuppress removes the suppress flag for a runID and returns the reason (empty if none).
func (h *AgentHandler) clearTTSSuppress(runID string) string {
	h.ttsSuppressMu.Lock()
	defer h.ttsSuppressMu.Unlock()
	reason := h.ttsSuppressReasons[runID]
	delete(h.ttsSuppressReasons, runID)
	return reason
}

// resolveRunID maps a gateway UUID to the device idempotencyKey, or returns runID unchanged.
func (h *AgentHandler) resolveRunID(runID string) string {
	h.runIDMapMu.Lock()
	defer h.runIDMapMu.Unlock()
	if mapped, ok := h.runIDMap[runID]; ok {
		return mapped
	}
	return runID
}

// mapRunID records that a gateway UUID belongs to the given device idempotencyKey.
func (h *AgentHandler) mapRunID(openclawID, deviceID string) {
	h.runIDMapMu.Lock()
	defer h.runIDMapMu.Unlock()
	h.runIDMap[openclawID] = deviceID
	if len(h.runIDMap) > 200 {
		for k := range h.runIDMap {
			delete(h.runIDMap, k)
			break
		}
	}
}

// assistantFirstDeltaAt observes timing without consuming streaming state.
func (h *AgentHandler) assistantFirstDeltaAt(runID string) time.Time {
	h.streamStatsMu.Lock()
	defer h.streamStatsMu.Unlock()
	if s := h.streamStats[runID]; s != nil {
		return s.assistantFirstAt
	}
	return time.Time{}
}

// assistantElapsedMs omits unknown timing rather than inventing a first token.
func (s *runStreamStats) assistantElapsedMs(at time.Time) (int64, bool) {
	if s == nil || s.assistantFirstAt.IsZero() || at.Before(s.assistantFirstAt) {
		return 0, false
	}
	return at.Sub(s.assistantFirstAt).Milliseconds(), true
}
