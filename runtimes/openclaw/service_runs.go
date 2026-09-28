package openclaw

import (
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/flow"
)

// SetSessionKey stores the session key for outgoing chat messages.
func (s *OpenclawService) SetSessionKey(key string) {
	s.lastSessionKey.Store(key)
	slog.Info("session key stored", "component", "openclaw", "key", key)
	flow.Log("session_key_acquired", map[string]any{"key_len": len(key)})
}

// GetSessionKey returns the last observed session key, or empty string if none.
func (s *OpenclawService) GetSessionKey() string {
	v, _ := s.lastSessionKey.Load().(string)
	return v
}

// MarkGuardRun marks a runID as guard-active so the SSE handler broadcasts the response.
func (s *OpenclawService) MarkGuardRun(runID string, snapshotPath string) {
	s.guardRunsMu.Lock()
	s.guardRuns[runID] = snapshotPath
	s.guardRunsMu.Unlock()
	slog.Info("guard run marked", "component", "openclaw", "runID", runID, "snapshot", snapshotPath)
}

// ConsumeGuardRun checks and removes a guard-active runID.
func (s *OpenclawService) ConsumeGuardRun(runID string) (string, bool) {
	s.guardRunsMu.Lock()
	snap, ok := s.guardRuns[runID]
	if ok {
		delete(s.guardRuns, runID)
	}
	s.guardRunsMu.Unlock()
	return snap, ok
}

// poseBucketRunTTL bounds how long an unconsumed pose-bucket marker stays around.
const poseBucketRunTTL = 10 * time.Minute

// MarkPoseBucketRun stores the bucket + worst-snapshot filenames for a motion.activity turn.
func (s *OpenclawService) MarkPoseBucketRun(runID string, bucketID string, worstFilenames []string) {
	if runID == "" || bucketID == "" {
		return
	}
	clean := make([]string, 0, len(worstFilenames))
	for _, f := range worstFilenames {
		f = strings.TrimSpace(f)
		if f != "" {
			clean = append(clean, f)
		}
	}
	s.poseBucketRunsMu.Lock()
	s.prunePoseBucketRunsLocked()
	s.poseBucketRuns[runID] = poseBucketInfo{
		bucketID:  bucketID,
		filenames: clean,
		markedAt:  time.Now(),
	}
	s.poseBucketRunsMu.Unlock()
	slog.Info("pose bucket run marked",
		"component", "openclaw", "runID", runID, "bucket", bucketID, "worst_count", len(clean))
}

// ConsumePoseBucketRun returns the bucket info for a runID and deletes the entry.
func (s *OpenclawService) ConsumePoseBucketRun(runID string) (string, []string, bool) {
	s.poseBucketRunsMu.Lock()
	defer s.poseBucketRunsMu.Unlock()
	s.prunePoseBucketRunsLocked()
	info, ok := s.poseBucketRuns[runID]
	if !ok {
		return "", nil, false
	}
	delete(s.poseBucketRuns, runID)
	return info.bucketID, info.filenames, true
}

// prunePoseBucketRunsLocked drops marker entries older than poseBucketRunTTL.
func (s *OpenclawService) prunePoseBucketRunsLocked() {
	if len(s.poseBucketRuns) == 0 {
		return
	}
	cutoff := time.Now().Add(-poseBucketRunTTL)
	for k, v := range s.poseBucketRuns {
		if v.markedAt.Before(cutoff) {
			delete(s.poseBucketRuns, k)
		}
	}
}

// MarkBroadcastRun marks a runID so the agent's response is broadcast to all channels.
func (s *OpenclawService) MarkBroadcastRun(runID string) {
	s.broadcastRunsMu.Lock()
	s.broadcastRuns[runID] = true
	s.broadcastRunsMu.Unlock()
	slog.Info("broadcast run marked", "component", "openclaw", "runID", runID)
}

// ConsumeBroadcastRun checks and removes a broadcast-marked runID.
func (s *OpenclawService) ConsumeBroadcastRun(runID string) bool {
	s.broadcastRunsMu.Lock()
	ok := s.broadcastRuns[runID]
	if ok {
		delete(s.broadcastRuns, runID)
	}
	s.broadcastRunsMu.Unlock()
	return ok
}

// MarkWebChatRun marks a runID as originating from the web monitor chat.
func (s *OpenclawService) MarkWebChatRun(runID string) {
	s.webChatRunsMu.Lock()
	s.webChatRuns[runID] = true
	s.webChatRunsMu.Unlock()
	slog.Info("web chat run marked — TTS will be suppressed", "component", "openclaw", "runID", runID)
}

// IsWebChatRun checks if a runID is a web chat run (non-consuming).
func (s *OpenclawService) IsWebChatRun(runID string) bool {
	s.webChatRunsMu.Lock()
	ok := s.webChatRuns[runID]
	s.webChatRunsMu.Unlock()
	return ok
}

// ConsumeWebChatRun checks and removes a web-chat-marked runID.
func (s *OpenclawService) ConsumeWebChatRun(runID string) bool {
	s.webChatRunsMu.Lock()
	ok := s.webChatRuns[runID]
	if ok {
		delete(s.webChatRuns, runID)
	}
	s.webChatRunsMu.Unlock()
	return ok
}

// MarkSilentRun marks a run whose spoken reply must be suppressed (e.g. voice_agent_handled).
func (s *OpenclawService) MarkSilentRun(runID string) {
	s.silentRunsMu.Lock()
	s.silentRuns[runID] = true
	s.silentRunsMu.Unlock()
	slog.Info("silent run marked — TTS will be suppressed", "component", "openclaw", "runID", runID)
}

// IsSilentRun checks if a runID is a silent run (non-consuming).
func (s *OpenclawService) IsSilentRun(runID string) bool {
	s.silentRunsMu.Lock()
	ok := s.silentRuns[runID]
	s.silentRunsMu.Unlock()
	return ok
}

// ConsumeSilentRun checks and removes a silent-marked runID.
func (s *OpenclawService) ConsumeSilentRun(runID string) bool {
	s.silentRunsMu.Lock()
	ok := s.silentRuns[runID]
	if ok {
		delete(s.silentRuns, runID)
	}
	s.silentRunsMu.Unlock()
	return ok
}

// pendingChatTTL bounds how long an unclaimed pending trace stays around.
const pendingChatTTL = 2 * time.Minute

// Telemetry retains queued task evidence without extending routing or busy state.
const pendingTaskTTL = 24 * time.Hour
const pendingTaskMaxEntries = 1024

// pendingSendBusyWindow is the freshness window used by IsBusy() to treat a just-sent chat.send as "busy" even before lifecycle_start echoes back.
const pendingSendBusyWindow = 30 * time.Second

// pruneStalePendingChatLocked drops entries older than pendingChatTTL.
func (s *OpenclawService) pruneStalePendingChatLocked() {
	if len(s.pendingChatBuf) == 0 {
		return
	}
	cutoff := time.Now().Add(-pendingChatTTL)
	kept := s.pendingChatBuf[:0]
	for _, p := range s.pendingChatBuf {
		if p.sentAt.After(cutoff) {
			kept = append(kept, p)
		}
	}
	s.pendingChatBuf = kept
}

// HasFreshPendingChatSend returns true if any chat.send was issued within pendingSendBusyWindow but has not yet been paired with lifecycle_start.
func (s *OpenclawService) HasFreshPendingChatSend() bool {
	s.pendingChatMu.Lock()
	defer s.pendingChatMu.Unlock()
	cutoff := time.Now().Add(-pendingSendBusyWindow)
	for _, p := range s.pendingChatBuf {
		if p.sentAt.After(cutoff) {
			return true
		}
	}
	return false
}

// SetPendingChatTrace records an outbound chat.send so that a later UUID lifecycle can be mapped back via MatchPendingByMessage.
func (s *OpenclawService) SetPendingChatTrace(runID string, message string) {
	s.pendingChatMu.Lock()
	s.pruneStalePendingChatLocked()
	s.pendingChatBuf = append(s.pendingChatBuf, pendingTrace{
		runID:   runID,
		message: message,
		sentAt:  time.Now(),
	})
	s.prunePendingTasksLocked()
	if len(s.pendingTaskBuf) >= pendingTaskMaxEntries {
		copy(s.pendingTaskBuf, s.pendingTaskBuf[len(s.pendingTaskBuf)-pendingTaskMaxEntries+1:])
		s.pendingTaskBuf = s.pendingTaskBuf[:pendingTaskMaxEntries-1]
	}
	s.pendingTaskBuf = append(s.pendingTaskBuf, pendingTrace{runID: runID, message: message, sentAt: time.Now()})
	s.pendingChatMu.Unlock()
}

// RemovePendingChatTraceByRunID removes the entry whose runID matches target.
func (s *OpenclawService) RemovePendingChatTraceByRunID(target string) bool {
	if target == "" {
		return false
	}
	s.pendingChatMu.Lock()
	defer s.pendingChatMu.Unlock()
	s.pruneStalePendingChatLocked()
	s.prunePendingTasksLocked()
	for i, p := range s.pendingTaskBuf {
		if p.runID == target {
			s.pendingTaskBuf = append(s.pendingTaskBuf[:i], s.pendingTaskBuf[i+1:]...)
			break
		}
	}
	for i, p := range s.pendingChatBuf {
		if p.runID == target {
			s.pendingChatBuf = append(s.pendingChatBuf[:i], s.pendingChatBuf[i+1:]...)
			return true
		}
	}
	return false
}

// MatchPendingByMessage finds and removes the pending entry whose message matches needle (after trim).
func (s *OpenclawService) MatchPendingByMessage(needle string) string {
	needle = strings.TrimSpace(stripJevPreload(strings.TrimSpace(needle)))
	if needle == "" {
		return ""
	}
	s.pendingChatMu.Lock()
	defer s.pendingChatMu.Unlock()
	s.pruneStalePendingChatLocked()
	if len(s.pendingChatBuf) == 0 {
		return ""
	}
	prefixLen := len(needle)
	if prefixLen > 256 {
		prefixLen = 256
	}
	needlePrefix := needle[:prefixLen]

	bestIdx := -1
	for i, p := range s.pendingChatBuf {
		stored := strings.TrimSpace(p.message)
		if stored == needle {
			bestIdx = i
			break
		}
		if bestIdx < 0 && len(stored) >= prefixLen && stored[:prefixLen] == needlePrefix {
			bestIdx = i
		}
	}
	if bestIdx < 0 {
		return ""
	}
	matched := s.pendingChatBuf[bestIdx].runID
	s.pendingChatBuf = append(s.pendingChatBuf[:bestIdx], s.pendingChatBuf[bestIdx+1:]...)
	return matched
}

// prunePendingTasksLocked bounds telemetry evidence independently of routing.
func (s *OpenclawService) prunePendingTasksLocked() {
	cutoff := time.Now().Add(-pendingTaskTTL)
	kept := s.pendingTaskBuf[:0]
	for _, p := range s.pendingTaskBuf {
		if p.sentAt.After(cutoff) {
			kept = append(kept, p)
		}
	}
	s.pendingTaskBuf = kept
}

// MatchPendingTaskByMessage consumes only a unique exact trimmed match for telemetry.
func (s *OpenclawService) MatchPendingTaskByMessage(needle string) string {
	needle = strings.TrimSpace(needle)
	if needle == "" {
		return ""
	}
	s.pendingChatMu.Lock()
	defer s.pendingChatMu.Unlock()
	s.prunePendingTasksLocked()
	bestIdx := -1
	for i, p := range s.pendingTaskBuf {
		if strings.TrimSpace(p.message) == needle {
			if bestIdx >= 0 {
				return ""
			}
			bestIdx = i
		}
	}
	if bestIdx < 0 {
		return ""
	}
	matched := s.pendingTaskBuf[bestIdx].runID
	s.pendingTaskBuf = append(s.pendingTaskBuf[:bestIdx], s.pendingTaskBuf[bestIdx+1:]...)
	return matched
}
