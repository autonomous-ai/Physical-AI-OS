package codex

import (
	"log/slog"

	"go.autonomous.ai/os/system/domain"
)

// CompactSession — Codex does not expose a compact API; rotate via
// NewSession instead.
func (s *CodexService) CompactSession(sessionKey string) error {
	slog.Info("CompactSession: not supported (codex backend)", "component", "codex", "session", sessionKey)
	return domain.ErrNotSupportedByRuntime
}

// codexFallbackTokenThreshold is a safety net only: Codex auto-compacts its
// own context (model_auto_compact_token_limit), so the reported per-turn
// input stays bounded and this rarely fires.
const codexFallbackTokenThreshold = 120_000

// ShouldRotateSession rotates on the live CONTEXT size — the raw
// `input_tokens` of the last turn.completed, which on the Responses API is the
// whole prompt including its cached prefix (s.lastContextTokens, stashed in
// translator.go).
func (s *CodexService) ShouldRotateSession(totalTokens, _ int) bool {
	contextTokens := int(s.lastContextTokens.Load())
	if contextTokens == 0 {
		contextTokens = totalTokens
	}
	return contextTokens > codexFallbackTokenThreshold
}

// NewSession tells the bridge to drop the persisted thread id (session.new
// frame) so the next `codex exec` starts a fresh thread, and clears the local
// session key.
func (s *CodexService) NewSession(sessionKey string) error {
	slog.Info("NewSession: requesting fresh codex thread", "component", "codex", "key", sessionKey)
	s.sessionUUID.Store("")
	s.lastContextTokens.Store(0)
	if err := s.sendFrame(map[string]any{"type": "session.new"}); err != nil {
		slog.Warn("session.new frame send failed (bridge will retry fresh on resume failure)",
			"component", "codex", "error", err)
	}
	return nil
}
