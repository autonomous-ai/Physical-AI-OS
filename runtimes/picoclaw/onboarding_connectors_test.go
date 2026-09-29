package picoclaw

import (
	"strings"
	"testing"
)

// TestAgentsMDBlock_RoutesConnectorsSkill guards the connectors-routing rule in
// PicoClaw's AGENTS.md block.
func TestAgentsMDBlock_RoutesConnectorsSkill(t *testing.T) {
	if !strings.Contains(agentsMDBlock, "`connectors` skill") {
		t.Fatalf("agentsMDBlock missing connectors-skill routing rule:\n%s", agentsMDBlock)
	}
	if !strings.Contains(agentsMDBlock, "configs/") {
		t.Fatalf("agentsMDBlock connectors rule must reference the on-disk credential path:\n%s", agentsMDBlock)
	}
	if !strings.Contains(strings.ToLower(agentsMDBlock), "never install") &&
		!strings.Contains(strings.ToLower(agentsMDBlock), "never write your own") {
		t.Fatalf("agentsMDBlock connectors rule must forbid installing/writing an alternative client:\n%s", agentsMDBlock)
	}
}
