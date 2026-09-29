package picoclaw

import (
	"encoding/json"
	"testing"
)

// applyObserverHook must register the process hook under hooks.processes.<name>,
// turn the hooks.enabled gate on, and carry the substituted URL — mirroring the
// config.json shape PicoClaw's process-hook loader expects.
func TestApplyObserverHook(t *testing.T) {
	cfg := map[string]any{}
	const url = "http://127.0.0.1:5000/api/agent/channel-turn"
	applyObserverHook(cfg, observerHookScriptPath, url)

	hooks, ok := cfg["hooks"].(map[string]any)
	if !ok {
		t.Fatalf("cfg.hooks missing/not a map: %v", cfg["hooks"])
	}
	if hooks["enabled"] != true {
		t.Errorf("hooks.enabled = %v, want true (gate must be on)", hooks["enabled"])
	}
	procs, ok := hooks["processes"].(map[string]any)
	if !ok {
		t.Fatalf("hooks.processes missing/not a map: %v", hooks["processes"])
	}
	entry, ok := procs[observerHookName].(map[string]any)
	if !ok {
		t.Fatalf("hooks.processes.%s missing: %v", observerHookName, procs)
	}
	if entry["enabled"] != true {
		t.Errorf("entry.enabled = %v, want true (per-process gate — without it PicoClaw skips the hook)", entry["enabled"])
	}
	if entry["transport"] != "stdio" {
		t.Errorf("transport = %v, want stdio", entry["transport"])
	}
	cmd, ok := entry["command"].([]any)
	if !ok || len(cmd) != 2 || cmd[0] != "python3" || cmd[1] != observerHookScriptPath {
		t.Errorf("command = %v, want [python3 %s]", entry["command"], observerHookScriptPath)
	}
	env, ok := entry["env"].(map[string]any)
	if !ok || env["OS_SERVER_TURN_URL"] != url {
		t.Errorf("env.OS_SERVER_TURN_URL = %v, want %s", env["OS_SERVER_TURN_URL"], url)
	}
	// OBSERVER_DEBUG must be written so it survives an os-server restart's config
	// rewrite (a hand-added key is wiped because the whole entry map is replaced).
	if env["OBSERVER_DEBUG"] != "1" {
		t.Errorf("env.OBSERVER_DEBUG = %v, want \"1\"", env["OBSERVER_DEBUG"])
	}
	if obs, ok := entry["observe"].([]any); !ok || len(obs) != 2 {
		t.Errorf("observe = %v, want [turn_start turn_end]", entry["observe"])
	}
	if _, present := entry["intercept"]; present {
		t.Errorf("intercept should be absent (observe-only), got %v", entry["intercept"])
	}
}

// Re-applying to an already-configured map must be a no-op (same marshaled bytes),
// so a steady boot reports changed=false and forces no gateway restart.
func TestApplyObserverHookIdempotent(t *testing.T) {
	const url = "http://127.0.0.1:5000/api/agent/channel-turn"
	cfg := map[string]any{}
	applyObserverHook(cfg, observerHookScriptPath, url)
	first, _ := json.Marshal(cfg)
	applyObserverHook(cfg, observerHookScriptPath, url)
	second, _ := json.Marshal(cfg)
	if string(first) != string(second) {
		t.Errorf("re-apply changed config:\n first=%s\nsecond=%s", first, second)
	}
}

// applyObserverHook must preserve unrelated config (e.g. tools.mcp) — it only owns
// the hooks subtree.
func TestApplyObserverHookPreservesOtherKeys(t *testing.T) {
	cfg := map[string]any{
		"tools": map[string]any{"mcp": map[string]any{"enabled": true}},
	}
	applyObserverHook(cfg, observerHookScriptPath, "http://127.0.0.1:5000/api/agent/channel-turn")
	tools, ok := cfg["tools"].(map[string]any)
	if !ok {
		t.Fatalf("tools subtree lost: %v", cfg)
	}
	if mcp, ok := tools["mcp"].(map[string]any); !ok || mcp["enabled"] != true {
		t.Errorf("tools.mcp clobbered: %v", tools["mcp"])
	}
}
