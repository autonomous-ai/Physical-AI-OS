package openclaw

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/server/config"
)

// TestAgentsMDBlock_RoutesConnectorsSkill guards that the AGENTS.md block routes email/calendar to the connectors skill.
func TestAgentsMDBlock_RoutesConnectorsSkill(t *testing.T) {
	if !strings.Contains(agentsMDBlock, "`connectors` skill") {
		t.Fatalf("agentsMDBlock missing connectors-skill routing rule:\n%s", agentsMDBlock)
	}
	// Must name the on-disk credential location so the agent never concludes a token-only service (Gmail/Calendar/Drive) is unconnected.
	if !strings.Contains(agentsMDBlock, "configs/") {
		t.Fatalf("agentsMDBlock connectors rule must reference the on-disk credential path:\n%s", agentsMDBlock)
	}
	if !strings.Contains(strings.ToLower(agentsMDBlock), "never install") &&
		!strings.Contains(strings.ToLower(agentsMDBlock), "never write your own") {
		t.Fatalf("agentsMDBlock connectors rule must forbid installing/writing an alternative client:\n%s", agentsMDBlock)
	}
}

// TestEnsureAgentsMDBlock_InjectsConnectorsRule checks the block lands in AGENTS.md idempotently.
func TestEnsureAgentsMDBlock_InjectsConnectorsRule(t *testing.T) {
	configDir := t.TempDir()
	workspace := filepath.Join(configDir, "workspace")
	if err := os.MkdirAll(workspace, 0o755); err != nil {
		t.Fatal(err)
	}
	// Seed a minimal AGENTS.md with the anchor line so injection targets it instead of shelling out to `openclaw setup`.
	agentsFile := filepath.Join(workspace, "AGENTS.md")
	if err := os.WriteFile(agentsFile, []byte("# Agent\n\nYour workspace is here.\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	s := &OpenclawService{config: &config.Config{OpenclawConfigDir: configDir}}

	modified, err := s.ensureAgentsMDBlock()
	if err != nil {
		t.Fatalf("ensureAgentsMDBlock: %v", err)
	}
	if !modified {
		t.Fatal("expected AGENTS.md to be modified on first inject")
	}

	got, err := os.ReadFile(agentsFile)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(got), "`connectors` skill") {
		t.Fatalf("AGENTS.md missing connectors rule after inject:\n%s", got)
	}

	modified2, err := s.ensureAgentsMDBlock()
	if err != nil {
		t.Fatalf("ensureAgentsMDBlock (2nd): %v", err)
	}
	if modified2 {
		t.Fatal("expected no modification on idempotent re-run")
	}
	if n := strings.Count(string(got), osMandatoryMarker); n != 1 {
		t.Fatalf("expected exactly one OS block marker, got %d", n)
	}
}
