package openclaw

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestJevPluginSyncPreservesOptOutAndIsIdempotent(t *testing.T) {
	home := t.TempDir()
	file := filepath.Join(home, "openclaw.json")
	original := []byte(`{"plugins":{"entries":{"autonomous-jev":{"enabled":false,"config":{"enabled":false}}}}}`)
	if err := os.WriteFile(file, original, 0600); err != nil {
		t.Fatal(err)
	}
	changed, err := syncJevPlugin(home, "/private/config.json")
	if err != nil || changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
	if changed, err = syncJevPlugin(home, "/private/config.json"); err != nil || changed {
		t.Fatalf("second changed=%v err=%v", changed, err)
	}
	got, _ := os.ReadFile(file)
	if string(got) != string(original) {
		t.Fatal("operator config changed")
	}
	if _, err := os.Stat(filepath.Join(home, "extensions")); !os.IsNotExist(err) {
		t.Fatal("disabled build installed plugin assets", err)
	}
}

func TestJevPreloadRunCorrelation(t *testing.T) {
	data, _ := json.Marshal(map[string]any{"version": 1, "skill": "harness-use", "path": "/workspace/skills/harness-use/SKILL.md", "content": "complete skill"})
	prefix := "[jev-skill-preload]\n" + string(data) + "\n[/jev-skill-preload]\n\n"
	s := &OpenclawService{}
	s.SetPendingChatTrace("run", "make report")
	if got := s.MatchPendingByMessage(prefix + "make report"); got != "run" {
		t.Fatalf("run=%q", got)
	}
	for _, text := range []string{"[jev-skill-preload]\ninvalid\n[/jev-skill-preload]\n\nhello", "[jev-skill-preload]\n" + strings.Repeat("x", 65537) + "\n[/jev-skill-preload]\n\nhello"} {
		if got := stripJevPreload(text); got != text {
			t.Fatal("invalid envelope stripped")
		}
	}
}

func TestJevPluginBuildSwitchOverridesConfig(t *testing.T) {
	for _, raw := range []string{`{"keep":true,"plugins":{"entries":{"autonomous-jev":{"enabled":true,"config":{"enabled":true,"keep":true}}}}}`} {
		home := t.TempDir()
		file := filepath.Join(home, "openclaw.json")
		if err := os.WriteFile(file, []byte(raw), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := syncJevPlugin(home, "/private/config.json"); err != nil {
			t.Fatal(err)
		}
		data, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		var cfg map[string]any
		if err := json.Unmarshal(data, &cfg); err != nil {
			t.Fatal(err)
		}
		entry := cfg["plugins"].(map[string]any)["entries"].(map[string]any)[jevPluginID].(map[string]any)
		if cfg["keep"] != true || entry["enabled"] != jevEnabled || entry["config"].(map[string]any)["enabled"] != jevEnabled {
			t.Fatal(cfg)
		}
		if changed, err := syncJevPlugin(home, "/private/config.json"); err != nil || changed {
			t.Fatalf("second changed=%v err=%v", changed, err)
		}
	}
}

func TestJevDisabledFreshInstallIsNoop(t *testing.T) {
	for _, raw := range []string{"", `{"keep":true}`, `{"plugins":{"entries":{"other":{"enabled":true}}}}`} {
		home := t.TempDir()
		file := filepath.Join(home, "openclaw.json")
		if raw != "" {
			if err := os.WriteFile(file, []byte(raw), 0600); err != nil {
				t.Fatal(err)
			}
		}
		// An invalid provider path must not matter while disabled.
		if changed, err := syncJevPlugin(home, "relative-invalid"); err != nil || changed {
			t.Fatalf("changed=%v err=%v", changed, err)
		}
		entries, err := os.ReadDir(home)
		if err != nil {
			t.Fatal(err)
		}
		want := 0
		if raw != "" {
			want = 1
		}
		if len(entries) != want {
			t.Fatal("disabled build created assets", entries)
		}
		if raw != "" {
			data, _ := os.ReadFile(file)
			if string(data) != raw {
				t.Fatal("disabled build changed fresh config")
			}
		}
	}
}
