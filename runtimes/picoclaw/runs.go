package picoclaw

import (
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/flow"
)

// SetSessionKey stores the session id.
func (s *PicoclawService) SetSessionKey(key string) {
	s.sessionUUID.Store(key)
	slog.Info("session key stored", "component", "picoclaw", "key", key)
	flow.Log("session_key_acquired", map[string]any{"key_len": len(key)})
}

// GetSessionKey returns the PicoClaw session id or "".
func (s *PicoclawService) GetSessionKey() string {
	v, _ := s.sessionUUID.Load().(string)
	return v
}

func (s *PicoclawService) MarkGuardRun(runID string, snapshotPath string) {
	s.guardRunsMu.Lock()
	s.guardRuns[runID] = snapshotPath
	s.guardRunsMu.Unlock()
	slog.Info("guard run marked", "component", "picoclaw", "runID", runID, "snapshot", snapshotPath)
}

func (s *PicoclawService) ConsumeGuardRun(runID string) (string, bool) {
	s.guardRunsMu.Lock()
	snap, ok := s.guardRuns[runID]
	if ok {
		delete(s.guardRuns, runID)
	}
	s.guardRunsMu.Unlock()
	return snap, ok
}

const poseBucketRunTTL = 10 * time.Minute

func (s *PicoclawService) MarkPoseBucketRun(runID string, bucketID string, worstFilenames []string) {
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
		"component", "picoclaw", "runID", runID, "bucket", bucketID, "worst_count", len(clean))
}

func (s *PicoclawService) ConsumePoseBucketRun(runID string) (string, []string, bool) {
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

func (s *PicoclawService) prunePoseBucketRunsLocked() {
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

func (s *PicoclawService) MarkBroadcastRun(runID string) {
	s.broadcastRunsMu.Lock()
	s.broadcastRuns[runID] = true
	s.broadcastRunsMu.Unlock()
	slog.Info("broadcast run marked", "component", "picoclaw", "runID", runID)
}

func (s *PicoclawService) ConsumeBroadcastRun(runID string) bool {
	s.broadcastRunsMu.Lock()
	ok := s.broadcastRuns[runID]
	if ok {
		delete(s.broadcastRuns, runID)
	}
	s.broadcastRunsMu.Unlock()
	return ok
}

func (s *PicoclawService) MarkWebChatRun(runID string) {
	s.webChatRunsMu.Lock()
	s.webChatRuns[runID] = true
	s.webChatRunsMu.Unlock()
	slog.Info("web chat run marked — TTS will be suppressed", "component", "picoclaw", "runID", runID)
}

func (s *PicoclawService) IsWebChatRun(runID string) bool {
	s.webChatRunsMu.Lock()
	ok := s.webChatRuns[runID]
	s.webChatRunsMu.Unlock()
	return ok
}

func (s *PicoclawService) ConsumeWebChatRun(runID string) bool {
	s.webChatRunsMu.Lock()
	ok := s.webChatRuns[runID]
	if ok {
		delete(s.webChatRuns, runID)
	}
	s.webChatRunsMu.Unlock()
	return ok
}

func (s *PicoclawService) MarkSilentRun(runID string) {
	s.silentRunsMu.Lock()
	s.silentRuns[runID] = true
	s.silentRunsMu.Unlock()
	slog.Info("silent run marked — TTS will be suppressed", "component", "picoclaw", "runID", runID)
}

func (s *PicoclawService) IsSilentRun(runID string) bool {
	s.silentRunsMu.Lock()
	ok := s.silentRuns[runID]
	s.silentRunsMu.Unlock()
	return ok
}

func (s *PicoclawService) ConsumeSilentRun(runID string) bool {
	s.silentRunsMu.Lock()
	ok := s.silentRuns[runID]
	if ok {
		delete(s.silentRuns, runID)
	}
	s.silentRunsMu.Unlock()
	return ok
}

const pendingChatTTL = 2 * time.Minute
const pendingSendBusyWindow = 30 * time.Second

func (s *PicoclawService) pruneStalePendingChatLocked() {
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

func (s *PicoclawService) HasFreshPendingChatSend() bool {
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

func (s *PicoclawService) SetPendingChatTrace(runID string, message string) {
	s.pendingChatMu.Lock()
	s.pruneStalePendingChatLocked()
	s.pendingChatBuf = append(s.pendingChatBuf, pendingTrace{
		runID:   runID,
		message: message,
		sentAt:  time.Now(),
	})
	s.pendingChatMu.Unlock()
}

func (s *PicoclawService) RemovePendingChatTraceByRunID(target string) bool {
	if target == "" {
		return false
	}
	s.pendingChatMu.Lock()
	defer s.pendingChatMu.Unlock()
	s.pruneStalePendingChatLocked()
	for i, p := range s.pendingChatBuf {
		if p.runID == target {
			s.pendingChatBuf = append(s.pendingChatBuf[:i], s.pendingChatBuf[i+1:]...)
			return true
		}
	}
	return false
}

func (s *PicoclawService) MatchPendingByMessage(needle string) string {
	needle = strings.TrimSpace(needle)
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
