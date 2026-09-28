package gatewayd

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

// This test exercises the real runtime adapter, native catalog, Jev protocol
// and outgoing model-input renderer with only a local HTTP mock provider.
func TestJevProviderToModelInput(t *testing.T) {
	for _, key := range []string{"CODEX_CONFIG", "CLAUDE_CONFIG_DIR", "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG_DIR", "XDG_CONFIG_HOME"} {
		t.Setenv(key, "")
	}
	root := t.TempDir()
	cfg := Config{Home: root, Workspace: filepath.Join(root, "workspace"), JevConfigPath: filepath.Join(root, "config.json")}
	t.Setenv("CLAUDE_CONFIG_DIR", filepath.Join(cfg.Home, ".claude"))
	if err := os.MkdirAll(cfg.Workspace, 0700); err != nil {
		t.Fatal(err)
	}
	skills := filepath.Join(cfg.Home, ".claude", "skills")
	skillDir := filepath.Join(skills, "calendar")
	if err := os.MkdirAll(skillDir, 0700); err != nil {
		t.Fatal(err)
	}
	body := "BEGIN_COMPLETE_SKILL_" + strings.Repeat("reference-details-", 800) + "_END_COMPLETE_SKILL"
	skill := "---\nname: calendar\ndescription: Read calendar events\n---\n" + body
	if err := os.WriteFile(filepath.Join(skillDir, "SKILL.md"), []byte(skill), 0600); err != nil {
		t.Fatal(err)
	}
	// Match the ordinary generated native configuration; it must not disable Jev.
	policyPath := filepath.Join(cfg.Workspace, ".claude", "settings.json")
	if err := os.MkdirAll(filepath.Dir(policyPath), 0700); err != nil {
		t.Fatal(err)
	}
	nativeConfig := `{"enableAllProjectMcpServers":true}`
	if err := os.WriteFile(policyPath, []byte(nativeConfig), 0600); err != nil {
		t.Fatal(err)
	}
	var calls atomic.Int32
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		calls.Add(1)
		if req.URL.Path != "/jev/decisions" || req.Header.Get("Authorization") != "Bearer mock-only" {
			t.Error("wrong Jev endpoint/auth contract")
		}
		var payload struct {
			Model string
			State struct {
				Prompt     string
				Candidates []struct {
					ID          string
					Description string
				}
			}
			Questions map[string]struct{ Type string }
		}
		if err := json.NewDecoder(req.Body).Decode(&payload); err != nil {
			t.Error(err)
		}
		if payload.Model != "typesafe/jev-1.13" || payload.State.Prompt != "read my calendar" || len(payload.State.Candidates) != 1 || payload.State.Candidates[0].ID != "skill_0" || !strings.Contains(payload.State.Candidates[0].Description, "Read calendar events") || payload.Questions["skill"].Type != "choice" || payload.Questions["fit_skill_0"].Type != "noul" {
			t.Errorf("bad protocol payload: %#v", payload)
		}
		_, _ = w.Write([]byte(`{"answers":{"skill":{"type":"choice","choice":"skill_0","probabilities":{"none":0.05,"skill_0":0.95}},"fit_skill_0":{"type":"noul","noul":0.95}}}`))
	}))
	defer provider.Close()
	credentials, _ := json.Marshal(map[string]string{"llm_base_url": provider.URL, "llm_api_key": "mock-only"})
	if err := os.WriteFile(cfg.JevConfigPath, credentials, 0600); err != nil {
		t.Fatal(err)
	}
	// Even with valid credentials and installed skills, the default makes no request.
	defaultResult := New(cfg, nil).prepareSkill(context.Background(), turnPayload{Content: "read my calendar", Source: "user"})
	if defaultResult.preload != "" || calls.Load() != 0 {
		t.Fatal("default must not call Jev")
	}
	cfg.JevEnabled = true
	s := New(cfg, nil)

	s.sendUserMessage(turnPayload{Content: "read my calendar", Source: "user"})
	if len(s.pending) != 1 {
		t.Fatal("model input not queued")
	}
	var wire struct {
		Message struct {
			Content []struct {
				Type string
				Text string
			}
		}
	}
	if err := json.Unmarshal(s.pending[0], &wire); err != nil {
		t.Fatal(err)
	}
	if len(wire.Message.Content) != 2 || wire.Message.Content[1].Text != "read my calendar" {
		t.Fatalf("unexpected model input: %#v", wire)
	}
	output := wire.Message.Content[0].Text

	if calls.Load() != 1 || !strings.Contains(output, body) || !strings.Contains(output, skillDir) {
		t.Fatalf("complete installed skill not delivered: calls=%d bytes=%d", calls.Load(), len(output))
	}
	// Native policy changes take effect without rebuilding the router and must
	// prevent even sending candidate metadata to the provider.
	if err := os.WriteFile(policyPath, []byte(`{"permissions":{}}`), 0600); err != nil {
		t.Fatal(err)
	}
	blocked := s.prepareSkill(context.Background(), turnPayload{Content: "read my calendar", Source: "user"})
	if blocked.preload != "" || blocked.Content != "read my calendar" || calls.Load() != 1 {
		t.Fatal("native policy failed to prevent selection")
	}
	cfg.JevEnabled = false
	disabled := New(cfg, nil).prepareSkill(context.Background(), turnPayload{Content: "read my calendar", Source: "user"})
	if disabled.preload != "" || calls.Load() != 1 {
		t.Fatal("disabled adapter contacted provider")
	}
}
