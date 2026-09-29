package openclaw

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"go.autonomous.ai/os/system/server/config"
)

func newServiceWithDir(t *testing.T, dir string) *OpenclawService {
	t.Helper()
	return &OpenclawService{config: &config.Config{OpenclawConfigDir: dir}}
}

func newServiceWithDirAndKey(t *testing.T, dir, apiKey, baseURL string) *OpenclawService {
	t.Helper()
	return &OpenclawService{config: &config.Config{
		OpenclawConfigDir: dir,
		LLMAPIKey:         apiKey,
		LLMBaseURL:        baseURL,
	}}
}

func writeOpenclawJSON(t *testing.T, dir string, data map[string]interface{}) {
	t.Helper()
	b, err := json.MarshalIndent(data, "", "  ")
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "openclaw.json"), b, 0600); err != nil {
		t.Fatalf("write openclaw.json: %v", err)
	}
}

func readOpenclawJSON(t *testing.T, dir string) map[string]interface{} {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(dir, "openclaw.json"))
	if err != nil {
		t.Fatalf("read openclaw.json: %v", err)
	}
	var out map[string]interface{}
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatalf("parse openclaw.json: %v", err)
	}
	return out
}

func TestEnsureGatewayToken_SeedsWhenMissing(t *testing.T) {
	dir := t.TempDir()
	writeOpenclawJSON(t, dir, map[string]interface{}{
		"gateway": map[string]interface{}{"mode": "local"},
	})

	svc := newServiceWithDir(t, dir)
	changed, err := svc.ensureGatewayToken()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !changed {
		t.Fatal("expected changed=true when token was missing")
	}

	cfg := readOpenclawJSON(t, dir)
	gw, _ := cfg["gateway"].(map[string]interface{})
	auth, _ := gw["auth"].(map[string]interface{})
	token, _ := auth["token"].(string)
	mode, _ := auth["mode"].(string)

	if token == "" {
		t.Error("gateway.auth.token must be non-empty after seed")
	}
	if mode != "token" {
		t.Errorf("gateway.auth.mode must be %q, got %q", "token", mode)
	}
}

func TestEnsureGatewayToken_NoOpWhenPresent(t *testing.T) {
	dir := t.TempDir()
	existing := "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
	writeOpenclawJSON(t, dir, map[string]interface{}{
		"gateway": map[string]interface{}{
			"auth": map[string]interface{}{
				"token": existing,
				"mode":  "token",
			},
		},
	})

	svc := newServiceWithDir(t, dir)
	changed, err := svc.ensureGatewayToken()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if changed {
		t.Fatal("expected changed=false when token already present")
	}

	cfg := readOpenclawJSON(t, dir)
	gw, _ := cfg["gateway"].(map[string]interface{})
	auth, _ := gw["auth"].(map[string]interface{})
	token, _ := auth["token"].(string)

	if token != existing {
		t.Errorf("token must not be rotated: got %q, want %q", token, existing)
	}
}

func TestEnsureProviderConfig_NoOpWhenKeyMatches(t *testing.T) {
	dir := t.TempDir()
	const apiKey = "test-api-key"
	const baseURL = "https://api.example.com"
	writeOpenclawJSON(t, dir, map[string]interface{}{
		"models": map[string]interface{}{
			"providers": map[string]interface{}{
				"autonomous": map[string]interface{}{
					"apiKey":  apiKey,
					"baseUrl": baseURL,
				},
			},
		},
	})

	svc := newServiceWithDirAndKey(t, dir, apiKey, baseURL)
	changed, err := svc.ensureProviderConfig()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if changed {
		t.Fatal("expected changed=false when apiKey+baseUrl already match config")
	}
}

func TestEnsureProviderConfig_SkipsWhenNoAPIKey(t *testing.T) {
	dir := t.TempDir()
	writeOpenclawJSON(t, dir, map[string]interface{}{})

	svc := newServiceWithDir(t, dir)
	changed, err := svc.ensureProviderConfig()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if changed {
		t.Fatal("expected changed=false when LLMAPIKey is empty (device not configured)")
	}
}
