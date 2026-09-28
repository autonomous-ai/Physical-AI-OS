package http

import (
	"errors"
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/i18n"
)

// autoCompactCooldown is the minimum time between two compact triggers (compact can take 30-60s+).
const autoCompactCooldown = 2 * time.Minute

// autoNewSessionCooldown is the minimum time between two new-session triggers, so a token
// burst across consecutive lifecycle.end events cannot drop the session twice.
const autoNewSessionCooldown = 30 * time.Second

// maybeAutoCompact triggers a sessions.compact RPC when ShouldRotateSession fires.
// Currently unused (maybeAutoNewSession is wired in handler_events.go instead).
func (h *AgentHandler) maybeAutoCompact(sessionKey string, totalTokens int, flowRunID string) {
	// Mutually exclusive with maybeAutoNewSession: only one is wired, so turnsSinceRotation
	// is incremented exactly once per turn.
	turns := int(h.turnsSinceRotation.Add(1))
	if !h.agentGateway.ShouldRotateSession(totalTokens, turns) {
		return
	}
	if !h.compacting.CompareAndSwap(false, true) {
		return
	}
	h.turnsSinceRotation.Store(0)
	slog.Info("auto-compact triggered", "component", "agent",
		"total_tokens", totalTokens, "turns", turns)
	flow.Log("compact_triggered", map[string]any{
		"session": sessionKey,
		"tokens":  totalTokens,
	}, flowRunID)
	go func() {
		defer time.AfterFunc(autoCompactCooldown, func() {
			h.compacting.Store(false)
		})
		// Fixed phrase: HAL caches the WAV on first render.
		if err := hal.SpeakCachedInterruptible(i18n.One(i18n.PhraseCompactNotice)); err != nil {
			slog.Warn("compaction notice TTS failed", "component", "agent", "backend", h.agentGateway.Name(), "error", err)
		}
		if sessionKey == "" {
			slog.Error("auto-compact failed: no session key", "component", "agent")
			return
		}
		if err := h.agentGateway.CompactSession(sessionKey); err != nil {
			if errors.Is(err, domain.ErrNotSupportedByRuntime) {
				slog.Info("auto-compact skipped: backend has no compact API",
					"component", "agent", "backend", h.agentGateway.Name())
			} else {
				slog.Error("auto-compact failed", "component", "agent", "error", err)
			}
		}
	}()
}

// maybeAutoNewSession triggers a sessions.new RPC when ShouldRotateSession fires. Instant, unlike
// compact; loses in-session history but keeps device-side memory. No TTS notice.
func (h *AgentHandler) maybeAutoNewSession(sessionKey string, totalTokens int, flowRunID string) {
	turns := int(h.turnsSinceRotation.Add(1))
	// Rotation policy is per-backend (see domain.AgentGateway ShouldRotateSession).
	if !h.agentGateway.ShouldRotateSession(totalTokens, turns) {
		return
	}
	if !h.newSessioning.CompareAndSwap(false, true) {
		return
	}
	h.turnsSinceRotation.Store(0)
	slog.Info("auto-new-session triggered", "component", "agent",
		"total_tokens", totalTokens, "turns", turns)
	flow.Log("new_session_triggered", map[string]any{
		"session": sessionKey,
		"tokens":  totalTokens,
		"turns":   turns,
	}, flowRunID)
	go func() {
		defer time.AfterFunc(autoNewSessionCooldown, func() {
			h.newSessioning.Store(false)
		})
		if sessionKey == "" {
			slog.Error("auto-new-session failed: no session key", "component", "agent")
			return
		}
		if err := h.agentGateway.NewSession(sessionKey); err != nil {
			slog.Error("auto-new-session failed", "component", "agent", "error", err)
		}
	}()
}
