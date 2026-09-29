package claudecode

import (
	"log/slog"

	"go.autonomous.ai/os/system/domain"
)

// CompactSession — Claude Code auto-compacts its own context when it
// approaches the window limit; there is no external compact RPC to call.
func (s *ClaudeCodeService) CompactSession(sessionKey string) error {
	slog.Info("CompactSession: not supported (claudecode auto-compacts)", "component", "claudecode", "session", sessionKey)
	return domain.ErrNotSupportedByRuntime
}

// rotateMaxTurns / rotateTokenThreshold gate session rotation (see
// ShouldRotateSession).
const (
	rotateMaxTurns       = 80
	rotateTokenThreshold = 150_000
)

// ShouldRotateSession rotates on turn count (primary — persona re-anchor,
// see the constants above) or a token spike (safety net).
func (s *ClaudeCodeService) ShouldRotateSession(totalTokens, turnsSinceRotation int) bool {
	return turnsSinceRotation >= rotateMaxTurns || totalTokens > rotateTokenThreshold
}

// NewSession asks the bridge to restart Claude Code WITHOUT --resume, starting a
// fresh session; the local session id is dropped so the init event of the new
// session is adopted.
func (s *ClaudeCodeService) NewSession(sessionKey string) error {
	slog.Info("NewSession: requesting fresh claude session", "component", "claudecode", "key", sessionKey)
	s.sessionUUID.Store("")
	if err := s.sendFrame(map[string]any{"type": "session.new"}); err != nil {
		// Not connected — the bridge never saw the request, so its session.json
		// still resumes the old session on the next spawn.
		slog.Warn("NewSession: bridge not reachable", "component", "claudecode", "error", err)
	}
	return nil
}
