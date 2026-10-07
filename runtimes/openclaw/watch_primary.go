package openclaw

import (
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/fsnotify/fsnotify"
)

const osWriteFlagName = ".os-model-write-flag"
const primarySyncDebounce = 300 * time.Millisecond
const osWriteFlagWindow = 3 * time.Second
const primaryWatchRetryInterval = 5 * time.Second

// setOSWriteFlag writes expectedPrimary (e.g. "autonomous/claude-opus-4-6") into the flag file.
func setOSWriteFlag(configDir, expectedPrimary string) {
	flagPath := filepath.Join(configDir, osWriteFlagName)
	if err := os.WriteFile(flagPath, []byte(expectedPrimary), 0600); err != nil {
		slog.Warn("[primarysync] write flag failed", "path", flagPath, "err", err)
	}
}

// isOSWrite returns true when the flag file exists, its mtime is within osWriteFlagWindow, AND its content matches actualPrimary.
func isOSWrite(configDir, actualPrimary string) bool {
	flagPath := filepath.Join(configDir, osWriteFlagName)
	info, err := os.Stat(flagPath)
	if err != nil || time.Since(info.ModTime()) >= osWriteFlagWindow {
		return false
	}
	content, err := os.ReadFile(flagPath)
	if err != nil {
		return false
	}
	return strings.TrimSpace(string(content)) == actualPrimary
}

// clearOSWriteFlag removes the flag file after consuming it.
func clearOSWriteFlag(configDir string) {
	_ = os.Remove(filepath.Join(configDir, osWriteFlagName))
}

// StartPrimaryModelWatch watches the openclaw config directory for changes to openclaw.json.
func (s *OpenclawService) StartPrimaryModelWatch(ctx context.Context) {
	dir := s.config.OpenclawConfigDir

	for {
		if _, err := os.Stat(dir); err == nil {
			break
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(primaryWatchRetryInterval):
		}
	}

	watcher, err := fsnotify.NewWatcher()
	if err != nil {
		slog.Error("[primarysync] create watcher failed", "err", err)
		return
	}
	defer watcher.Close()

	if err := watcher.Add(dir); err != nil {
		slog.Error("[primarysync] watch dir failed", "dir", dir, "err", err)
		return
	}
	slog.Info("[primarysync] watching openclaw config dir for primary model changes", "dir", dir)

	var debounceTimer *time.Timer
	resetDebounce := func() {
		if debounceTimer != nil {
			debounceTimer.Stop()
		}
		debounceTimer = time.AfterFunc(primarySyncDebounce, func() {
			s.syncPrimaryFromFile()
		})
	}

	for {
		select {
		case <-ctx.Done():
			if debounceTimer != nil {
				debounceTimer.Stop()
			}
			return
		case event, ok := <-watcher.Events:
			if !ok {
				return
			}
			if filepath.Base(event.Name) != "openclaw.json" {
				continue
			}
			if event.Has(fsnotify.Write) || event.Has(fsnotify.Create) || event.Has(fsnotify.Rename) {
				resetDebounce()
			}
		case err, ok := <-watcher.Errors:
			if !ok {
				return
			}
			slog.Warn("[primarysync] watcher error", "err", err)
		}
	}
}

// syncPrimaryFromFile is the debounced handler that fires after openclaw.json changes.
func (s *OpenclawService) syncPrimaryFromFile() {
	// Serialize concurrent invocations (debounce timer fires in its own goroutine and may overlap with UpdatePrimaryModel or other config paths).
	s.primarySyncMu.Lock()
	defer s.primarySyncMu.Unlock()
	if s.config.LLMRuntimeManaged() {
		return
	}

	configDir := s.config.OpenclawConfigDir
	configPath := filepath.Join(configDir, "openclaw.json")

	raw, err := os.ReadFile(configPath)
	if err != nil {
		slog.Warn("[primarysync] read openclaw.json failed", "err", err)
		return
	}
	var cfg map[string]any
	if err := json.Unmarshal(raw, &cfg); err != nil {
		slog.Warn("[primarysync] parse openclaw.json failed", "err", err)
		return
	}

	primary := extractPrimaryModel(cfg)
	if primary == "" {
		return
	}

	if isOSWrite(configDir, primary) {
		clearOSWriteFlag(configDir)
		slog.Debug("[primarysync] skipping Lamp-initiated write", "primary", primary)
		return
	}

	provider, modelKey, ok := splitProviderModel(primary)
	if !ok || provider != customProviderName {
		slog.Warn("[primarysync] external primary switched to non-autonomous provider, Lamp config NOT updated (state drift)",
			"primary", primary, "os_model", s.config.LLMModelKey())
		return
	}

	// Read LLMModel under config.mu (LLMModelKey) to avoid a data race with concurrent WithLockSave calls from HTTP handlers.
	currentModel := s.config.LLMModelKey()
	if currentModel == modelKey {
		return
	}

	slog.Info("[primarysync] external model change detected, syncing to Lamp config",
		"old", currentModel, "new", modelKey)
	// SetLLMModel acquires the config mutex so this write cannot race with device.UpdateConfig's concurrent UpdateLLMModel + Save call.
	if err := s.config.SetLLMModel(modelKey); err != nil {
		slog.Error("[primarysync] save Lamp config failed", "err", err)
	}
}

// extractPrimaryModel drills into agents.defaults.model.primary in a parsed openclaw.json map and returns the value, or "" when any level is absent.
func extractPrimaryModel(cfg map[string]any) string {
	agents, _ := cfg["agents"].(map[string]any)
	if agents == nil {
		return ""
	}
	defaults, _ := agents["defaults"].(map[string]any)
	if defaults == nil {
		return ""
	}
	model, _ := defaults["model"].(map[string]any)
	if model == nil {
		return ""
	}
	primary, _ := model["primary"].(string)
	return primary
}

// splitProviderModel splits a "provider/model-key" string into its two parts.
func splitProviderModel(fullKey string) (provider, key string, ok bool) {
	idx := strings.IndexByte(fullKey, '/')
	if idx < 0 {
		return "", fullKey, false
	}
	return fullKey[:idx], fullKey[idx+1:], true
}
