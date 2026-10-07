package device

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

const wakeWordApplyTimeout = 30 * time.Second

// wakeWordApply serializes HTTP/MQTT saves and keeps failed applies retryable.
// pending is independent of the desired config, which can change even on a
// failed save. It is cleared only after a successful save and HAL restart.
type wakeWordApply struct {
	mu      sync.Mutex
	pending bool
}

// UpdateWakeWord persists the gate and waits for any required HAL restart.
// A failed save/restart leaves pending set so an unchanged retry can recover.
func (s *Service) UpdateWakeWord(enabled bool) error {
	s.wakeApply.mu.Lock()
	defer s.wakeApply.mu.Unlock()

	if err := s.config.WithLockSave(func(c *config.Config) {
		changed := applyWakeWord(c, enabled)
		s.wakeApply.pending = s.wakeApply.pending || changed
	}); err != nil {
		return fmt.Errorf("save config: %w", err)
	}

	if !s.wakeApply.pending {
		slog.Info("wakeword config unchanged", "component", "device", "enabled", enabled)
		return nil
	}
	slog.Info("wakeword config updated", "component", "device", "enabled", enabled)
	return s.applyPendingWakeWord()
}

// applyPendingWakeWord requires wakeApply.mu and a successful config save.
func (s *Service) applyPendingWakeWord() error {
	ctx, cancel := context.WithTimeout(context.Background(), wakeWordApplyTimeout)
	defer cancel()
	if err := s.restartHALAndWait(ctx, "wake/input-mode config change"); err != nil {
		return err
	}
	s.wakeApply.pending = false
	return nil
}

// applyWakeWord updates the flag and reports whether its effective value changed.
func applyWakeWord(c *config.Config, enabled bool) bool {
	changed := c.WakeWordEnabled() != enabled
	c.WakeWord = &enabled
	return changed
}

// UpdateVoiceInputMode shares the HTTP persistence/apply path and its retry state.
func (s *Service) UpdateVoiceInputMode(mode string) error {
	return s.UpdateConfig(domain.UpdateConfigRequest{VoiceInputMode: &mode})
}
