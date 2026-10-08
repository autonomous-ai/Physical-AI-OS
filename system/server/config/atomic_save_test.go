package config

import (
	"os"
	"path/filepath"
	"testing"
)

// A truncated or garbled config.json must not crash-loop os-server: the bad
// file is kept aside and the device boots unconfigured, which re-opens setup.
func TestProvideConfig_CorruptFileStartsSetupInsteadOfPanicking(t *testing.T) {
	origPath := configPath
	configPath = filepath.Join(t.TempDir(), "config.json")
	defer func() { configPath = origPath }()

	for _, body := range []string{"", `{"set_up_completed": tr`} {
		if err := os.WriteFile(configPath, []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
		cfg := ProvideConfig()
		if cfg.SetUpCompleted {
			t.Errorf("%q: SetUpCompleted = true, want a fresh unconfigured config", body)
		}
		kept, err := os.ReadFile(configPath + corruptSuffix)
		if err != nil || string(kept) != body {
			t.Errorf("%q: corrupt copy = %q, %v; want the original bytes", body, kept, err)
		}
		if _, err := Load(); err != nil {
			t.Errorf("%q: config.json not rewritten as valid JSON: %v", body, err)
		}
	}
}

// Saves replace config.json by rename and leave no temp files behind.
func TestSave_ReplacesFileAtomically(t *testing.T) {
	origPath := configPath
	dir := t.TempDir()
	configPath = filepath.Join(dir, "config.json")
	defer func() { configPath = origPath }()

	cfg := Default()
	cfg.DeviceID = "first"
	if err := cfg.Save(); err != nil {
		t.Fatal(err)
	}
	if err := cfg.WithLockSave(func(c *Config) { c.DeviceID = "second" }); err != nil {
		t.Fatal(err)
	}
	got, err := Load()
	if err != nil || got.DeviceID != "second" {
		t.Fatalf("Load = %q, %v; want second", got.DeviceID, err)
	}
	info, err := os.Stat(configPath)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("config.json mode = %v, want 0600", info.Mode().Perm())
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		t.Fatalf("dir holds %d entries, want only config.json: %v", len(entries), entries)
	}
}
