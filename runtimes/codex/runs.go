package codex

import (
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/flow"
)

// SetSessionKey stores the session id.
func (s *CodexService) SetSessionKey(key string) {
	s.sessionUUID.Store(key)
	slog.Info("session key stored", "component", "codex", "key", key)
	flow.Log("session_key_acquired", map[string]any{"key_len": len(key)})
}

// GetSessionKey returns the Codex session id or "".
func (s *CodexService) GetSessionKey() string {
	v, _ := s.sessionUUID.Load().(string)
	return v
}

func (s *CodexService) MarkGuardRun(runID string, snapshotPath string) {
	s.guardRunsMu.Lock()
	s.guardRuns[runID] = snapshotPath
	s.guardRunsMu.Unlock()
	slog.Info("guard run marked", "component", "codex", "runID", runID, "snapshot", snapshotPath)
}

func (s *CodexService) ConsumeGuardRun(runID string) (string, bool) {
	s.guardRunsMu.Lock()
	snap, ok := s.guardRuns[runID]
	if ok {
		delete(s.guardRuns, runID)
	}
	s.guardRunsMu.Unlock()
	return snap, ok
}

const poseBucketRunTTL = 10 * time.Minute

func (s *CodexService) MarkPoseBucketRun(runID string, bucketID string, worstFilenames []string) {
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
		"component", "codex", "runID", runID, "bucket", bucketID, "worst_count", len(clean))
}

func (s *CodexService) ConsumePoseBucketRun(runID string) (string, []string, bool) {
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

func (s *CodexService) prunePoseBucketRunsLocked() {
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

func (s *CodexService) MarkBroadcastRun(runID string) {
	s.broadcastRunsMu.Lock()
	s.broadcastRuns[runID] = true
	s.broadcastRunsMu.Unlock()
	slog.Info("broadcast run marked", "component", "codex", "runID", runID)
}

func (s *CodexService) ConsumeBroadcastRun(runID string) bool {
	s.broadcastRunsMu.Lock()
	ok := s.broadcastRuns[runID]
	if ok {
		delete(s.broadcastRuns, runID)
	}
	s.broadcastRunsMu.Unlock()
	return ok
}

func (s *CodexService) MarkWebChatRun(runID string) {
	s.webChatRunsMu.Lock()
	s.webChatRuns[runID] = true
	s.webChatRunsMu.Unlock()
	slog.Info("web chat run marked — TTS will be suppressed", "component", "codex", "runID", runID)
}

func (s *CodexService) IsWebChatRun(runID string) bool {
	s.webChatRunsMu.Lock()
	ok := s.webChatRuns[runID]
	s.webChatRunsMu.Unlock()
	return ok
}

func (s *CodexService) ConsumeWebChatRun(runID string) bool {
	s.webChatRunsMu.Lock()
	ok := s.webChatRuns[runID]
	if ok {
		delete(s.webChatRuns, runID)
	}
	s.webChatRunsMu.Unlock()
	return ok
}

func (s *CodexService) MarkSilentRun(runID string) {
	s.silentRunsMu.Lock()
	if s.silentRuns == nil {
		s.silentRuns = make(map[string]bool)
	}
	s.silentRuns[runID] = true
	s.silentRunsMu.Unlock()
	slog.Info("silent run marked — TTS will be suppressed", "component", "codex", "runID", runID)
}

func (s *CodexService) IsSilentRun(runID string) bool {
	s.silentRunsMu.Lock()
	ok := s.silentRuns[runID]
	s.silentRunsMu.Unlock()
	return ok
}

func (s *CodexService) ConsumeSilentRun(runID string) bool {
	s.silentRunsMu.Lock()
	ok := s.silentRuns[runID]
	if ok {
		delete(s.silentRuns, runID)
	}
	s.silentRunsMu.Unlock()
	return ok
}

// markTelegramRun records a Telegram-originated turn (telegram_poll.go) so
// emitFinal can DM the reply back to the originating chat.
func (s *CodexService) markTelegramRun(runID string, chatID string) {
	if runID == "" || chatID == "" {
		return
	}
	s.telegramRunsMu.Lock()
	if s.telegramRuns == nil {
		s.telegramRuns = make(map[string]string)
	}
	s.telegramRuns[runID] = chatID
	s.telegramRunsMu.Unlock()
	slog.Info("telegram run marked — reply will be DMed", "component", "codex", "runID", runID, "chatID", chatID)
}

// hasTelegramRun reports (non-consuming) whether a Telegram-originated run is
// still awaiting its reply — the typing keeper polls this to know when to stop.
func (s *CodexService) hasTelegramRun(runID string) bool {
	s.telegramRunsMu.Lock()
	_, ok := s.telegramRuns[runID]
	s.telegramRunsMu.Unlock()
	return ok
}

// consumeTelegramRun is one-shot: returns the chat id for a Telegram-originated
// run and removes the entry, or "" when the run did not come from Telegram.
// Called by emitFinal (reply routing) and handleError (leak prevention).
func (s *CodexService) consumeTelegramRun(runID string) string {
	s.telegramRunsMu.Lock()
	chatID, ok := s.telegramRuns[runID]
	if ok {
		delete(s.telegramRuns, runID)
	}
	s.telegramRunsMu.Unlock()
	return chatID
}

// markSlackRun records a Slack-originated turn (slack.go) so emitFinal can
// post the reply back to the originating channel/thread.
func (s *CodexService) markSlackRun(runID string, origin slackRun) {
	if runID == "" || origin.channel == "" {
		return
	}
	s.slackRunsMu.Lock()
	if s.slackRuns == nil {
		s.slackRuns = make(map[string]slackRun)
	}
	s.slackRuns[runID] = origin
	s.slackRunsMu.Unlock()
	slog.Info("slack run marked — reply will be posted to slack",
		"component", "codex", "runID", runID, "channel", origin.channel)
}

// hasSlackRun reports (non-consuming) whether a Slack-originated run is still
// awaiting its reply — backs IsSlackOriginRun (domain.SlackBridge peek).
func (s *CodexService) hasSlackRun(runID string) bool {
	if runID == "" {
		return false
	}
	s.slackRunsMu.Lock()
	_, ok := s.slackRuns[runID]
	s.slackRunsMu.Unlock()
	return ok
}

// markDiscordRun records a Discord-originated turn (discord.go) so emitFinal
// can post the reply back to the originating channel.
func (s *CodexService) markDiscordRun(runID string, channelID string) {
	if runID == "" || channelID == "" {
		return
	}
	s.discordRunsMu.Lock()
	if s.discordRuns == nil {
		s.discordRuns = make(map[string]string)
	}
	s.discordRuns[runID] = channelID
	s.discordRunsMu.Unlock()
	slog.Info("discord run marked — reply will be posted to discord",
		"component", "codex", "runID", runID, "channelID", channelID)
}

// hasDiscordRun reports (non-consuming) whether a Discord-originated run is
// still awaiting its reply — the typing keeper polls this to know when to stop.
func (s *CodexService) hasDiscordRun(runID string) bool {
	if runID == "" {
		return false
	}
	s.discordRunsMu.Lock()
	_, ok := s.discordRuns[runID]
	s.discordRunsMu.Unlock()
	return ok
}

// consumeDiscordRun is one-shot: returns the channel id for a Discord-
// originated run and removes the entry, or "" when the run did not come from
// Discord.
// Called by emitFinal (reply routing) and handleError (leak prevention).
func (s *CodexService) consumeDiscordRun(runID string) string {
	s.discordRunsMu.Lock()
	channelID, ok := s.discordRuns[runID]
	if ok {
		delete(s.discordRuns, runID)
	}
	s.discordRunsMu.Unlock()
	return channelID
}

// consumeSlackRun is one-shot: returns the origin for a Slack-originated run
// and removes the entry.
// Called by emitFinal (reply routing), handleError (leak prevention) and DeliverSlackReply (safety
// net).
func (s *CodexService) consumeSlackRun(runID string) (slackRun, bool) {
	s.slackRunsMu.Lock()
	o, ok := s.slackRuns[runID]
	if ok {
		delete(s.slackRuns, runID)
	}
	s.slackRunsMu.Unlock()
	return o, ok
}

const pendingChatTTL = 2 * time.Minute
const pendingSendBusyWindow = 30 * time.Second

func (s *CodexService) pruneStalePendingChatLocked() {
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

func (s *CodexService) HasFreshPendingChatSend() bool {
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

func (s *CodexService) SetPendingChatTrace(runID string, message string) {
	s.pendingChatMu.Lock()
	s.pruneStalePendingChatLocked()
	s.pendingChatBuf = append(s.pendingChatBuf, pendingTrace{
		runID:   runID,
		message: message,
		sentAt:  time.Now(),
	})
	s.pendingChatMu.Unlock()
}

func (s *CodexService) RemovePendingChatTraceByRunID(target string) bool {
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

func (s *CodexService) MatchPendingByMessage(needle string) string {
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
