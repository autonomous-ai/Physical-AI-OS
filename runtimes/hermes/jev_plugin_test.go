package hermes

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestSyncJevPluginExistingDevice(t *testing.T) {
	home := t.TempDir()
	yamlPath := filepath.Join(home, "config.yaml")
	if err := os.WriteFile(yamlPath, []byte("model: kept\nplugins:\n  enabled: [other-plugin]\n  disabled: [unrelated]\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	osConfig := filepath.Join(t.TempDir(), "config.json")
	changed, err := syncJevPlugin(home, osConfig)
	if err != nil || !changed {
		t.Fatalf("first sync: %v, %v", changed, err)
	}
	dir := filepath.Join(home, "plugins", jevPluginName)
	for _, name := range []string{"plugin.yaml", "__init__.py", "router.py", "preload.py", "os-config-path.json"} {
		info, err := os.Stat(filepath.Join(dir, name))
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != 0o600 {
			t.Fatalf("%s permissions %v", name, info.Mode())
		}
	}
	data, err := os.ReadFile(filepath.Join(dir, "os-config-path.json"))
	if err != nil {
		t.Fatal(err)
	}
	var pathData map[string]string
	if err := json.Unmarshal(data, &pathData); err != nil {
		t.Fatal(err)
	}
	if pathData["config_path"] != osConfig || len(pathData) != 1 {
		t.Fatalf("sidecar: %v", pathData)
	}
	cfg, err := readHermesConfig(yamlPath)
	if err != nil {
		t.Fatal(err)
	}
	if cfg["model"] != "kept" {
		t.Fatal("unrelated config lost")
	}
	plugins := cfg["plugins"].(map[string]any)
	enabled := plugins["enabled"].([]any)
	if len(enabled) != 2 || enabled[0] != "other-plugin" || enabled[1] != jevPluginName {
		t.Fatalf("enabled: %v", enabled)
	}
	if plugins["disabled"].([]any)[0] != "unrelated" {
		t.Fatal("deny-list lost")
	}
	before, _ := os.Stat(filepath.Join(dir, "router.py"))
	changed, err = syncJevPlugin(home, osConfig)
	if err != nil || changed {
		t.Fatalf("idempotent sync: %v, %v", changed, err)
	}
	after, _ := os.Stat(filepath.Join(dir, "router.py"))
	if before.ModTime() != after.ModTime() {
		t.Fatal("unchanged code rewritten")
	}
	if err := os.WriteFile(filepath.Join(dir, "router.py"), []byte("old version"), 0o600); err != nil {
		t.Fatal(err)
	}
	changed, err = syncJevPlugin(home, osConfig)
	if err != nil || !changed {
		t.Fatalf("upgrade: %v, %v", changed, err)
	}
}

func TestSyncJevPluginAbsentAndDenied(t *testing.T) {
	home := t.TempDir()
	if changed, err := syncJevPlugin(home, "/config/config.json"); err != nil || changed {
		t.Fatalf("absent: %v %v", changed, err)
	}
	if _, err := os.Stat(filepath.Join(home, "plugins")); !os.IsNotExist(err) {
		t.Fatal("created plugins without Hermes")
	}
	path := filepath.Join(home, "config.yaml")
	original := []byte("plugins:\n  disabled: [jev]\n")
	if err := os.WriteFile(path, original, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := syncJevPlugin(home, "/config/config.json"); err != nil {
		t.Fatal(err)
	}
	actual, _ := os.ReadFile(path)
	if string(actual) != string(original) {
		t.Fatal("operator disable modified")
	}
}

func TestSyncJevPluginRejectsMalformedConfig(t *testing.T) {
	home := t.TempDir()
	path := filepath.Join(home, "config.yaml")
	original := []byte("plugins: invalid\n")
	if err := os.WriteFile(path, original, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := syncJevPlugin(home, "/config/config.json"); err == nil {
		t.Fatal("expected error")
	}
	actual, _ := os.ReadFile(path)
	if string(actual) != string(original) {
		t.Fatal("malformed operator config overwritten")
	}
}

func TestSyncJevPluginSpillBudget(t *testing.T) {
	for _, tc := range []struct {
		name, config string
		want         int
	}{
		{"already_enabled", "plugins:\n  enabled: [jev]\n", 131072},
		{"preserve_operator_limit", "plugins:\n  enabled: [jev]\nhooks:\n  output_spill:\n    max_chars: 7000\n", 7000},
	} {
		t.Run(tc.name, func(t *testing.T) {
			home := t.TempDir()
			path := filepath.Join(home, "config.yaml")
			if err := os.WriteFile(path, []byte(tc.config), 0600); err != nil {
				t.Fatal(err)
			}
			if _, err := syncJevPlugin(home, "/config/config.json"); err != nil {
				t.Fatal(err)
			}
			cfg, err := readHermesConfig(path)
			if err != nil {
				t.Fatal(err)
			}
			spill := cfg["hooks"].(map[string]any)["output_spill"].(map[string]any)
			// YAML decoders may represent integers with different concrete Go types.
			raw, _ := json.Marshal(spill["max_chars"])
			var got int
			if err := json.Unmarshal(raw, &got); err != nil || got != tc.want {
				t.Fatalf("max_chars: %s", raw)
			}
			if changed, err := syncJevPlugin(home, "/config/config.json"); err != nil || changed {
				t.Fatalf("not idempotent: %v %v", changed, err)
			}
		})
	}
}
