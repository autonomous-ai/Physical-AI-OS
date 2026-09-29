package device

import (
	"fmt"
	"log/slog"

	"go.autonomous.ai/os/system/server/config"
)

// UpdateWakeWord persists the top-level wakeword gate and restarts HAL only
// when its effective value changes, because HAL reads the flag at import time.
func (s *Service) UpdateWakeWord(enabled bool) error {
	changed := false
	if err := s.config.WithLockSave(func(c *config.Config) {
		changed = applyWakeWord(c, enabled)
	}); err != nil {
		return fmt.Errorf("save config: %w", err)
	}

	if !changed {
		slog.Info("wakeword config unchanged", "component", "device", "enabled", enabled)
		return nil
	}
	slog.Info("wakeword config updated", "component", "device", "enabled", enabled)
	s.restartHAL("wakeword config change")
	return nil
}

// applyWakeWord updates the flag and reports whether its effective value changed.
func applyWakeWord(c *config.Config, enabled bool) bool {
	changed := c.WakeWordEnabled() != enabled
	c.WakeWord = &enabled
	return changed
}
