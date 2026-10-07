package config

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestProvideConfigMissingWakeWordDoesNotRewriteOrRestartCompatibleConfig(t *testing.T) {
	dir := t.TempDir()
	origPath := configPath
	configPath = filepath.Join(dir, "config.json")
	defer func() { configPath = origPath }()

	data, err := json.Marshal(Default())
	if err != nil {
		t.Fatalf("marshal default config: %v", err)
	}
	var legacy map[string]any
	if err := json.Unmarshal(data, &legacy); err != nil {
		t.Fatalf("unmarshal default config: %v", err)
	}
	delete(legacy, "wakeword")
	delete(legacy, "voice_input_mode")
	data, err = json.Marshal(legacy)
	if err != nil {
		t.Fatalf("marshal legacy config: %v", err)
	}
	if err := os.WriteFile(configPath, data, 0o600); err != nil {
		t.Fatalf("write legacy config: %v", err)
	}

	cfg := ProvideConfig()
	if cfg.GetVoiceInputMode() != VoiceInputAutomatic {
		t.Fatal("missing voice_input_mode must default to automatic")
	}
	if cfg.WakeWordEnabled() {
		t.Fatal("missing wakeword must default to false")
	}
	after, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatalf("read config after ProvideConfig: %v", err)
	}
	if !bytes.Equal(after, data) {
		t.Fatalf("ProvideConfig rewrote a compatible config:\n got %s\nwant %s", after, data)
	}
}
