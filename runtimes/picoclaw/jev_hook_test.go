package picoclaw

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestJevDisabledFreshInstallIsNoop(t *testing.T) {
	for _, raw := range []string{"", `{"unrelated":true}`, `{"hooks":{"enabled":true,"processes":{"observer":{"enabled":true}}}}`} {
		dir := t.TempDir()
		file := filepath.Join(dir, "config.json")
		if raw != "" {
			if err := os.WriteFile(file, []byte(raw), 0600); err != nil {
				t.Fatal(err)
			}
		}
		if changed, err := syncJevHook(file, "relative-invalid", filepath.Join(dir, "hooks", "jev")); err != nil || changed {
			t.Fatalf("changed=%v err=%v", changed, err)
		}
		entries, err := os.ReadDir(dir)
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

func TestJevDisabledRetiresExistingHook(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "config.json")
	raw := `{"hooks":{"enabled":true,"processes":{"observer":{"enabled":true},"jev":{"enabled":true,"command":["python3","hook.py"]}}}}`
	if err := os.WriteFile(file, []byte(raw), 0600); err != nil {
		t.Fatal(err)
	}
	hookDir := filepath.Join(dir, "hooks", "jev")
	if changed, err := syncJevHook(file, "relative-invalid", hookDir); err != nil || !changed {
		t.Fatalf("changed=%v err=%v", changed, err)
	}
	cfg, err := readPicoclawConfig(file)
	if err != nil {
		t.Fatal(err)
	}
	hooks := cfg["hooks"].(map[string]any)
	processes := hooks["processes"].(map[string]any)
	if hooks["enabled"] != true || processes["observer"].(map[string]any)["enabled"] != true || processes["jev"].(map[string]any)["enabled"] != false {
		t.Fatal(cfg)
	}
	if changed, err := syncJevHook(file, "relative-invalid", hookDir); err != nil || changed {
		t.Fatalf("second changed=%v err=%v", changed, err)
	}
	if _, err := os.Stat(hookDir); !os.IsNotExist(err) {
		t.Fatal("disabled build installed hook", err)
	}
}

func TestJevHookOptOut(t *testing.T) {
	for _, raw := range []string{`{"hooks":{"enabled":false}}`, `{"hooks":{"enabled":true,"processes":{"jev":{"enabled":false}}}}`} {
		var cfg map[string]any
		json.Unmarshal([]byte(raw), &cfg)
		applyJevHook(cfg, "hook.py")
		hooks := cfg["hooks"].(map[string]any)
		if hooks["enabled"] == false {
			continue
		}
		if hooks["processes"].(map[string]any)["jev"].(map[string]any)["enabled"] != false {
			t.Fatal(cfg)
		}
	}
}

// Existing observer onboarding owns the global hook gate. Jev's explicit
// per-plugin opt-out survives that lifecycle without disabling the observer.
func TestJevOptOutSurvivesObserverOnboarding(t *testing.T) {
	cfg := map[string]any{"hooks": map[string]any{"enabled": false, "processes": map[string]any{"jev": map[string]any{"enabled": false}}}}
	applyObserverHook(cfg, "observer.py", "localhost")
	applyJevHook(cfg, "hook.py")
	hooks := cfg["hooks"].(map[string]any)
	if hooks["enabled"] != true || hooks["processes"].(map[string]any)["jev"].(map[string]any)["enabled"] != false {
		t.Fatal(cfg)
	}
}

func TestJevHookBuildSwitchOverridesConfig(t *testing.T) {
	for _, raw := range []string{`{}`, `{"hooks":{"enabled":true}}`, `{"hooks":{"processes":{"jev":{}}}}`, `{"hooks":{"processes":{"jev":{"enabled":"true"}}}}`, `{"hooks":{"enabled":true,"processes":{"jev":{"enabled":true}}}}`} {
		var cfg map[string]any
		if err := json.Unmarshal([]byte(raw), &cfg); err != nil {
			t.Fatal(err)
		}
		want := jevEnabled
		applyJevHook(cfg, "hook.py")
		hooks := cfg["hooks"].(map[string]any)
		if hooks["processes"].(map[string]any)["jev"].(map[string]any)["enabled"] != want {
			t.Fatal(cfg)
		}
		applyObserverHook(cfg, "observer.py", "localhost")
		applyJevHook(cfg, "hook.py")
		if hooks["processes"].(map[string]any)["jev"].(map[string]any)["enabled"] != want {
			t.Fatal("onboarding changed opt-in", cfg)
		}
	}
}
