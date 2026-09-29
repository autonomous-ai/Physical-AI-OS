package openclaw

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
)

// UpdatePrimaryModel patches agents.defaults.model.primary in openclaw.json to "autonomous/{modelKey}" and restarts the gateway so the change takes effect immediately.
func (s *OpenclawService) UpdatePrimaryModel(modelKey string) error {
	if modelKey == "" {
		return nil
	}

	s.primarySyncMu.Lock()
	defer s.primarySyncMu.Unlock()

	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	raw, err := os.ReadFile(configPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return fmt.Errorf("read openclaw config: %w", err)
	}

	var configData map[string]any
	if err := json.Unmarshal(raw, &configData); err != nil {
		return fmt.Errorf("parse openclaw config: %w", err)
	}

	newPrimary := customProviderName + "/" + modelKey
	if current := extractPrimaryModel(configData); current == newPrimary {
		return nil
	}

	agents := ensureMap(configData, "agents")
	defaults := ensureMap(agents, "defaults")
	modelMap := ensureMap(defaults, "model")
	modelMap["primary"] = newPrimary
	defaults["model"] = modelMap
	agents["defaults"] = defaults
	configData["agents"] = agents

	written, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal openclaw config: %w", err)
	}

	setOSWriteFlag(s.config.OpenclawConfigDir, newPrimary)

	if err := atomicWriteFile(configPath, written, 0600); err != nil {
		return fmt.Errorf("write openclaw config: %w", err)
	}
	if err := chownRuntimeUserIfRoot(configPath, openclawRuntimeUser); err != nil {
		slog.Warn("[model] chown openclaw config after primary update", "err", err)
	}

	slog.Info("[model] updated primary model in openclaw.json", "new", newPrimary)

	if err := restartOpenclawGateway(); err != nil {
		slog.Warn("[model] restart gateway after primary model update", "err", err)
	}
	return nil
}
