package picoclaw

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
)

// The observer hook forwards channel turns to /api/agent/channel-turn. PicoClaw does not scan a
// hooks dir: it is registered under config.json hooks.processes (hooks.enabled defaults false).

//go:embed resources/hooks/os-server-observer/observer.py
var observerHookScript []byte

const (
	observerHookName       = "os-server-observer"
	observerHookDir        = "/root/.picoclaw/hooks/os-server-observer"
	observerHookScriptPath = observerHookDir + "/observer.py"
	observerHookURLMark    = "__OS_SERVER_TURN_URL__"
	observerHookPort       = 5000 // fallback when config.HttpPort is unset (matches os-server default)
)

// ensureObserverHook materializes observer.py into the PicoClaw hooks dir (with the
// loopback URL substituted) and registers it in config.json.
// Returns true when the script OR the config changed — the gateway loads hooks only at start, so
// the caller (EnsureOnboarding) must restart it when this reports changed.
func (s *PicoclawService) ensureObserverHook() (bool, error) {
	url := s.observerHookURL()
	script := bytes.ReplaceAll(observerHookScript, []byte(observerHookURLMark), []byte(url))

	if err := os.MkdirAll(observerHookDir, 0o755); err != nil {
		return false, fmt.Errorf("mkdir hook dir: %w", err)
	}
	changedScript, err := writePicoFileIfChanged(observerHookScriptPath, script, 0o755)
	if err != nil {
		return false, err
	}

	changedCfg, err := s.ensureObserverHookConfig(url)
	if err != nil {
		return false, err
	}
	if changedScript || changedCfg {
		slog.Info("picoclaw observer hook materialized", "component", "picoclaw", "dir", observerHookDir, "url", url)
		return true, nil
	}
	return false, nil
}

// observerHookURL is the loopback ChannelTurn endpoint the subprocess POSTs to.
func (s *PicoclawService) observerHookURL() string {
	port := s.config.HttpPort
	if port == 0 {
		port = observerHookPort
	}
	return fmt.Sprintf("http://127.0.0.1:%d/api/agent/channel-turn", port)
}

// ensureObserverHookConfig upserts hooks.processes.os-server-observer in config.json
// and asserts hooks.enabled=true (gate defaults false, like tools.mcp.enabled).
func (s *PicoclawService) ensureObserverHookConfig(url string) (bool, error) {
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()

	path := picoclawConfigPath()
	cfg, err := readPicoclawConfig(path)
	if err != nil {
		return false, err
	}
	before, _ := json.Marshal(cfg)

	applyObserverHook(cfg, observerHookScriptPath, url)

	after, _ := json.Marshal(cfg)
	if bytes.Equal(before, after) {
		return false, nil
	}
	if err := writePicoclawConfig(path, cfg); err != nil {
		return false, err
	}
	slog.Info("picoclaw observer hook registered in config.json", "component", "picoclaw", "hook", observerHookName)
	return true, nil
}

// applyObserverHook upserts hooks.processes.os-server-observer in the decoded config
// map and asserts the hooks.enabled gate.
func applyObserverHook(cfg map[string]any, scriptPath, url string) {
	hooks := ensurePicoMap(cfg, "hooks")
	hooks["enabled"] = true // global gate — must be on to load ANY hook
	procs := ensurePicoMap(hooks, "processes")
	procs[observerHookName] = map[string]any{
		"enabled":   true,
		"transport": "stdio",
		"command":   []any{"python3", scriptPath},
		"env": map[string]any{
			"OS_SERVER_TURN_URL": url,
			"OBSERVER_DEBUG":     "1",
		},
		"observe": []any{"turn_start", "turn_end"},
	}
}

// writePicoFileIfChanged writes data to path (with perm) only when it differs from
// the current content, so a steady boot neither churns the file nor forces a gateway
// restart.
func writePicoFileIfChanged(path string, data []byte, perm os.FileMode) (bool, error) {
	if cur, err := os.ReadFile(path); err == nil && bytes.Equal(cur, data) {
		return false, nil
	}
	if err := os.WriteFile(path, data, perm); err != nil {
		return false, fmt.Errorf("write %s: %w", filepath.Base(path), err)
	}
	return true, nil
}
