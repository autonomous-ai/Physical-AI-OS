package gatewayd

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestJevTurnLocalPreparation(t *testing.T) {
	calls := 0
	s := &Server{preloadContext: func(_ context.Context, message string) string {
		calls++
		if message != "read my calendar" {
			t.Fatalf("selector received changed user text: %q", message)
		}
		return "full skill instructions"
	}}
	original := turnPayload{Content: "read my calendar", Source: "user"}
	prepared := s.prepareSkill(context.Background(), original)
	retry := s.prepareSkill(context.Background(), prepared)
	if calls != 1 || retry.promptWithSkill() != "full skill instructions\n\nread my calendar" {
		t.Fatalf("retry must reuse one preload: calls=%d payload=%#v", calls, retry)
	}
	if original.preload != "" || original.preloadChecked || original.Content != prepared.Content {
		t.Fatal("preparation mutated the original request")
	}
	s.preloadContext = func(context.Context, string) string { return "" }
	next := s.prepareSkill(context.Background(), turnPayload{Content: "hello"})
	if next.promptWithSkill() != "hello" {
		t.Fatal("empty/error fallback retained previous skill")
	}
}

func TestJevSkipsNonUserSourcesAndAttachments(t *testing.T) {
	s := &Server{preloadContext: func(context.Context, string) string { t.Fatal("unexpected selection"); return "" }}
	for _, source := range []string{"system", "user_slash"} {
		p := s.prepareSkill(context.Background(), turnPayload{Content: "sync skills", Source: source})
		if p.promptWithSkill() != "sync skills" {
			t.Fatal("system input changed")
		}
	}
	var p turnPayload
	p.Content = "look at this"
	p.Attachments = append(p.Attachments, struct {
		Type string `json:"type"`
		URL  string `json:"url"`
	}{Type: "image", URL: "data:image/png;base64,AA=="})
	if got := s.prepareSkill(context.Background(), p); len(got.Attachments) != 1 || got.preload != "" {
		t.Fatal("attachment flow changed")
	}
}

func TestJevNativePolicyAbstains(t *testing.T) {
	for _, key := range []string{"CODEX_CONFIG", "CLAUDE_CONFIG_DIR", "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG_DIR", "XDG_CONFIG_HOME"} {
		t.Setenv(key, "")
	}
	root := t.TempDir()
	cfg := Config{Home: root, Workspace: filepath.Join(root, "workspace")}
	if !nativePreloadAllowed(cfg) {
		t.Fatal("missing custom policy should allow selection")
	}
	path := filepath.Join(cfg.Home, ".claude", "settings.json")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(`{"permissions":{"deny":["Skill(calendar)"]}}`), 0600); err != nil {
		t.Fatal(err)
	}
	if nativePreloadAllowed(cfg) {
		t.Fatal("native skill policy was bypassed")
	}
	if err := os.WriteFile(path, []byte("malformed = ["), 0600); err != nil {
		t.Fatal(err)
	}
	if nativePreloadAllowed(cfg) {
		t.Fatal("malformed native config must abstain")
	}
	if err := os.WriteFile(path, []byte(`{"model":"skills-permissions-agent"}`), 0600); err != nil {
		t.Fatal(err)
	}
	if !nativePreloadAllowed(cfg) {
		t.Fatal("ordinary config string falsely treated as policy")
	}

	if !strings.Contains(path, root) {
		t.Fatal("test escaped temporary root")
	}
}

func TestJevClaudeQueuedWireContainsPreloadOnce(t *testing.T) {
	calls := 0
	s := &Server{preloadContext: func(context.Context, string) string { calls++; return "skill content" }}
	s.sendUserMessage(turnPayload{Content: "read my calendar"})
	if calls != 1 || len(s.pending) != 1 {
		t.Fatalf("calls=%d pending=%d", calls, len(s.pending))
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
	if len(wire.Message.Content) != 2 || wire.Message.Content[0].Text != "skill content" || wire.Message.Content[1].Text != "read my calendar" {
		t.Fatalf("unexpected child input: %#v", wire)
	}
	// The child restart flushes these already-rendered bytes; it does not select again.
	if strings.Count(string(s.pending[0]), "skill content") != 1 {
		t.Fatal("duplicate skill content")
	}
}

func TestJevInheritedPolicyAndProjectCatalogUseNativeLoader(t *testing.T) {
	for _, key := range []string{"CODEX_CONFIG", "CLAUDE_CONFIG_DIR", "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG_DIR", "XDG_CONFIG_HOME"} {
		t.Setenv(key, "")
	}
	root := t.TempDir()
	cfg := Config{Home: filepath.Join(root, "home"), Workspace: filepath.Join(root, "project", "nested")}
	path := filepath.Join(root, "project", ".claude", "settings.json")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(`{"permissions":{}}`), 0600); err != nil {
		t.Fatal(err)
	}
	if nativePreloadAllowed(cfg) {
		t.Fatal("ancestor native policy was ignored")
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	catalog := filepath.Join(root, "project", ".claude", "skills")
	if err := os.MkdirAll(catalog, 0700); err != nil {
		t.Fatal(err)
	}
	if nativePreloadAllowed(cfg) {
		t.Fatal("project skill overrides were ignored")
	}
}

func TestJevBuildSwitchIgnoresEnvironment(t *testing.T) {
	for _, value := range []string{"", "0", "false", "true", "invalid", "1"} {
		t.Run("value_"+value, func(t *testing.T) {
			t.Setenv("JEV_SKILL_PRELOAD", value)
			if got := configFromEnv().JevEnabled; got != jevEnabled {
				t.Fatalf("value %q enabled=%v", value, got)
			}
		})
	}
}

// A disabled build must not construct a selector or even alter turn bookkeeping.
func TestJevDisabledIsNoop(t *testing.T) {
	t.Setenv("JEV_SKILL_PRELOAD", "1")
	cfg := configFromEnv()
	if cfg.JevEnabled {
		t.Fatal("unvalidated runtime is enabled")
	}
	cfg.JevConfigPath = "/nonexistent/jev-config.json"
	s := New(cfg, nil)
	if s.preloadContext != nil {
		t.Fatal("disabled runtime constructed a selector")
	}
	for _, content := range []string{"read my calendar", "[voice-instruction] read my calendar [transcript] read my calendar", "brighter"} {
		original := turnPayload{Content: content, Source: "user"}
		got := s.prepareSkill(nil, original)
		if got.Content != original.Content || got.Source != original.Source || got.preload != "" || got.preloadChecked || got.promptWithSkill() != original.Content {
			t.Fatalf("disabled turn changed: %#v", got)
		}
	}
}
