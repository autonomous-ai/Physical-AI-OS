package codex

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Tests for the global user AGENTS.md block (ensureUserAgentsMDBlockAt): codex
// loads $CODEX_HOME/AGENTS.md in every session regardless of cwd, so the
// device-wide connector/skill rules must live there to reach coding sessions

// (a) fresh file → block written, idempotent on second call; (b) skill/connector
// references are ABSOLUTE so they resolve from any cwd.
func TestUserAgentsMD_InjectIdempotentAndAbsolutePaths(t *testing.T) {
	path := filepath.Join(t.TempDir(), "AGENTS.md")

	if !ensureUserAgentsMDBlockAt(path) {
		t.Fatal("first call: expected modified=true (block written)")
	}
	got := readFile(t, path)
	if !strings.Contains(got, osMandatoryMarker) {
		t.Error("block marker missing from user AGENTS.md")
	}
	if !strings.Contains(got, "/root/.codex/skills/connectors/SKILL.md") {
		t.Error("connectors skill must be referenced by its native-discovery absolute path (resolves from any cwd)")
	}
	if strings.Contains(got, "workspace/skills") {
		t.Error("must not reference workspace/skills — codex never scans it; skills live in $CODEX_HOME/skills")
	}
	if strings.Contains(got, "\n- skills/") || strings.Contains(got, "`skills/") {
		t.Error("no relative `skills/…` path may appear — coding sessions run in another cwd")
	}

	if ensureUserAgentsMDBlockAt(path) {
		t.Error("second call: expected modified=false (block already current)")
	}
}

// (c) pre-existing owner content below the block is preserved; a stale marked
// block is replaced, not duplicated.
func TestUserAgentsMD_PreservesOwnerContentAndReplacesStale(t *testing.T) {
	path := filepath.Join(t.TempDir(), "AGENTS.md")
	const ownerLine = "My own global preferences: prefer tabs."
	staleBlock := osMandatoryMarker + "\n**Old rule that should be dropped.**\n---\n"
	if err := os.WriteFile(path, []byte(staleBlock+"\n"+ownerLine+"\n"), 0o644); err != nil {
		t.Fatalf("seed file: %v", err)
	}

	if !ensureUserAgentsMDBlockAt(path) {
		t.Fatal("expected modified=true (stale block replaced)")
	}
	got := readFile(t, path)
	if !strings.Contains(got, ownerLine) {
		t.Error("owner content below the block must be preserved")
	}
	if strings.Contains(got, "Old rule that should be dropped") {
		t.Error("stale marked block must be stripped before re-injecting")
	}
	if strings.Count(got, osMandatoryMarker) != 1 {
		t.Errorf("expected exactly one OS marker, got %d", strings.Count(got, osMandatoryMarker))
	}
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return string(b)
}
