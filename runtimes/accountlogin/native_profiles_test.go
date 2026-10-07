package accountlogin

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/goccy/go-yaml"
)

func putNativeJSON(t *testing.T, path string, value any) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
}

func TestHermesModelPreservesRuntimeConfig(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte("model:\n  provider: custom:autonomous\n  default: Auto-AI\n  api_key: old-key\n  temperature: 0.5\ntools:\n  enabled: [terminal]\ncustom_providers:\n  - name: autonomous\n"), 0600); err != nil {
		t.Fatal(err)
	}
	data, err := mergeHermesModel(path, "anthropic", "claude-sonnet-4-6", "https://api.anthropic.com")
	if err != nil {
		t.Fatal(err)
	}
	var config map[string]any
	if err := yaml.Unmarshal(data, &config); err != nil {
		t.Fatal(err)
	}
	model := nativeMap(config, "model")
	if model["provider"] != "anthropic" || model["temperature"] == nil || config["tools"] == nil || config["custom_providers"] == nil {
		t.Fatalf("unexpected config: %s", data)
	}
	if _, ok := model["api_key"]; ok {
		t.Fatal("stale explicit API key survived account activation")
	}
}

func TestHermesFreshOAuthRequired(t *testing.T) {
	path := filepath.Join(t.TempDir(), "auth.json")
	if _, err := readHermesOAuth(path, "anthropic"); err == nil {
		t.Fatal("missing credential accepted")
	}
	putNativeJSON(t, path, map[string]any{"credential_pool": map[string]any{"anthropic": []any{map[string]any{"auth_type": "api_key", "access_token": "key", "refresh_token": "refresh"}}}})
	if _, err := readHermesOAuth(path, "anthropic"); err == nil {
		t.Fatal("API key accepted as account login")
	}
	putNativeJSON(t, path, map[string]any{"credential_pool": map[string]any{"anthropic": []any{map[string]any{"auth_type": "oauth", "access_token": "token", "refresh_token": "refresh"}}}})
	if _, err := readHermesOAuth(path, "anthropic"); err != nil {
		t.Fatal(err)
	}
}

func TestOpenClawAccountMergePreservesWorkspaceAndOtherAuth(t *testing.T) {
	stage, root := t.TempDir(), t.TempDir()
	rel := filepath.Join(".openclaw", "openclaw.json")
	putNativeJSON(t, filepath.Join(stage, rel), map[string]any{"agents": map[string]any{"defaults": map[string]any{"model": map[string]any{"primary": "openai/gpt-test"}, "models": map[string]any{"openai/gpt-test": map[string]any{}}}}, "auth": map[string]any{"profiles": map[string]any{"openai:new": map[string]any{"provider": "openai", "mode": "oauth"}}}})
	putNativeJSON(t, filepath.Join(root, rel), map[string]any{"gateway": map[string]any{"port": 18789}, "agents": map[string]any{"defaults": map[string]any{"workspace": "/workspace", "model": map[string]any{"primary": "autonomous/Auto-AI", "fallbacks": []any{"autonomous/Auto-AI"}}, "models": map[string]any{"autonomous/Auto-AI": map[string]any{}}}}})
	authRel := filepath.Join(".openclaw", "agents", "main", "agent", "auth-profiles.json")
	putNativeJSON(t, filepath.Join(stage, authRel), map[string]any{"version": 1, "profiles": map[string]any{"openai:new": map[string]any{"type": "oauth", "provider": "openai", "access": "test-access", "refresh": "test-refresh"}}})
	putNativeJSON(t, filepath.Join(root, authRel), map[string]any{"version": 1, "profiles": map[string]any{"other": map[string]any{"type": "api_key", "provider": "other", "key": "keep"}}})
	changes, err := openClawChanges(stage, root, "openai")
	if err != nil {
		t.Fatal(err)
	}
	var config map[string]any
	if err := json.Unmarshal(changes[filepath.Join(root, rel)], &config); err != nil {
		t.Fatal(err)
	}
	defaults := nativeMap(nativeMap(config, "agents"), "defaults")
	if defaults["workspace"] != "/workspace" || config["gateway"] == nil || nativeMap(defaults, "models")["autonomous/Auto-AI"] == nil {
		t.Fatal("unrelated config lost")
	}
	if nativeMap(defaults, "model")["fallbacks"] != nil {
		t.Fatal("OS proxy fallback survived account activation")
	}
	var auth map[string]any
	if err := json.Unmarshal(changes[filepath.Join(root, authRel)], &auth); err != nil {
		t.Fatal(err)
	}
	if len(nativeMap(auth, "profiles")) != 2 {
		t.Fatal("other provider auth lost")
	}
	// Preparation/verification must not install anything into the live runtime.
	live, err := readJSON(filepath.Join(root, rel))
	if err != nil {
		t.Fatal(err)
	}
	if nativeMap(nativeMap(nativeMap(live, "agents"), "defaults"), "model")["primary"] != "autonomous/Auto-AI" {
		t.Fatal("live model changed before install")
	}
}

func TestOpenClawClaudeRequiresNativeRoute(t *testing.T) {
	stage, root := t.TempDir(), t.TempDir()
	putNativeJSON(t, filepath.Join(stage, ".openclaw", "openclaw.json"), map[string]any{"agents": map[string]any{"defaults": map[string]any{"model": map[string]any{"primary": "anthropic/claude-test"}}}})
	putNativeJSON(t, filepath.Join(stage, ".credentials.json"), map[string]any{"claudeAiOauth": map[string]any{"accessToken": "access", "refreshToken": "refresh", "expiresAt": time.Now().Add(time.Hour).UnixMilli()}})
	putNativeJSON(t, filepath.Join(stage, ".claude.json"), map[string]any{"oauthAccount": map[string]any{"accountUuid": "test"}})
	if _, err := openClawChanges(stage, root, "anthropic"); err == nil {
		t.Fatal("missing CLI route accepted")
	}
	putNativeJSON(t, filepath.Join(stage, ".openclaw", "openclaw.json"), map[string]any{"agents": map[string]any{"defaults": map[string]any{"model": map[string]any{"primary": "anthropic/claude-test"}, "models": map[string]any{"anthropic/claude-test": map[string]any{"agentRuntime": map[string]any{"id": "claude-cli"}}}}}})
	if _, err := openClawChanges(stage, root, "anthropic"); err != nil {
		t.Fatal(err)
	}
}
