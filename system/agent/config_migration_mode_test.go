package agent

import (
	"os"
	"path/filepath"
	"testing"

	migrateconfig "go.autonomous.ai/os/system/agent/migrate_config"
	"go.autonomous.ai/os/system/server/config"
)

func TestNativeLLMModeSkipsMigrationAndAdvancesMarker(t *testing.T) {
	t.Chdir(t.TempDir())
	source := t.TempDir()
	target := t.TempDir()
	sourceConfig := `{"models":{"providers":{"autonomous":{"apiKey":"source-key","baseUrl":"https://source.invalid"}}}}`
	if err := os.WriteFile(filepath.Join(source, "openclaw.json"), []byte(sourceConfig), 0600); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(target, "config.toml")
	native := []byte("model = \"personal-model\"\n")
	if err := os.WriteFile(path, native, 0600); err != nil {
		t.Fatal(err)
	}
	cfg := &config.Config{AgentRuntime: "codex", LLMConfigAppliedRuntime: "openclaw", LLMConfigMode: "runtime", LLMAPIKey: "saved-os-key"}
	migration := &ConfigMigration{cfg: cfg, opts: migrateconfig.Options{OpenclawConfigDir: source, CodexHome: target}}
	migration.Reconcile()
	if cfg.LLMConfigAppliedRuntime != "codex" || cfg.LLMAPIKey != "saved-os-key" {
		t.Fatal("native switch did not preserve OS key and advance marker")
	}
	// Switching ownership back must not perform a delayed source migration.
	cfg.LLMConfigMode = "os"
	migration.Reconcile()
	got, err := os.ReadFile(path)
	if err != nil || string(got) != string(native) {
		t.Fatalf("native config overwritten: %q, %v", got, err)
	}
	if _, err := os.Stat(filepath.Join(target, ".env")); !os.IsNotExist(err) {
		t.Fatalf("migration wrote target credentials: %v", err)
	}
}
