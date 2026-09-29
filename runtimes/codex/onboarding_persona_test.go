package codex

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Tests for the persona inline block (ensurePersonaInlineBlockIn): codex auto-loads
// only AGENTS.md, so the SOUL.md persona + IDENTITY.md name are inlined at the top.

func writeWS(t *testing.T, dir, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
		t.Fatalf("write %s: %v", name, err)
	}
}

func readWS(t *testing.T, dir, name string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(dir, name))
	if err != nil {
		t.Fatalf("read %s: %v", name, err)
	}
	return string(b)
}

const personaTestAgents = "# Workspace\n\nYour workspace notes live here.\n"

func setupPersonaWorkspace(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	writeWS(t, dir, "SOUL.md", "# Soul\n\nBe warm, playful, and concise.\n")
	writeWS(t, dir, "IDENTITY.md", "# Identity\n\n- **Name:** Nova\n")
	writeWS(t, dir, "AGENTS.md", personaTestAgents)
	return dir
}

// (a) SOUL+IDENTITY present → block at top with name + soul text; second call is a
// no-op; (d) pre-existing AGENTS.md content preserved below the block.
func TestPersonaInline_InjectIdempotentAndPreservesContent(t *testing.T) {
	dir := setupPersonaWorkspace(t)

	modified, err := ensurePersonaInlineBlockIn(dir)
	if err != nil {
		t.Fatalf("first call: %v", err)
	}
	if !modified {
		t.Fatal("first call: expected modified=true")
	}

	got := readWS(t, dir, "AGENTS.md")
	if !strings.HasPrefix(got, personaInlineStart) {
		t.Errorf("block not at very top:\n%s", got)
	}
	if !strings.Contains(got, personaInlineEnd) {
		t.Error("end marker missing")
	}
	if !strings.Contains(got, "Your name is **Nova**.") {
		t.Errorf("name line missing:\n%s", got)
	}
	if !strings.Contains(got, "Be warm, playful, and concise.") {
		t.Error("soul text missing")
	}
	if !strings.HasSuffix(got, personaTestAgents) {
		t.Errorf("pre-existing AGENTS.md content not preserved below the block:\n%s", got)
	}

	// Idempotent: second call must not modify nor rewrite bytes.
	modified, err = ensurePersonaInlineBlockIn(dir)
	if err != nil {
		t.Fatalf("second call: %v", err)
	}
	if modified {
		t.Error("second call: expected modified=false (idempotent)")
	}
	if again := readWS(t, dir, "AGENTS.md"); again != got {
		t.Error("second call changed file bytes")
	}
}

// (b) SOUL.md changes → block updates in place.
func TestPersonaInline_SoulChangeUpdatesBlock(t *testing.T) {
	dir := setupPersonaWorkspace(t)
	if _, err := ensurePersonaInlineBlockIn(dir); err != nil {
		t.Fatalf("seed call: %v", err)
	}

	writeWS(t, dir, "SOUL.md", "# Soul\n\nBe serious and precise.\n")
	modified, err := ensurePersonaInlineBlockIn(dir)
	if err != nil {
		t.Fatalf("update call: %v", err)
	}
	if !modified {
		t.Fatal("expected modified=true after SOUL.md change")
	}

	got := readWS(t, dir, "AGENTS.md")
	if !strings.Contains(got, "Be serious and precise.") {
		t.Error("updated soul text missing")
	}
	if strings.Contains(got, "Be warm, playful, and concise.") {
		t.Error("stale soul text still present")
	}
	if strings.Count(got, personaInlineStart) != 1 {
		t.Error("expected exactly one persona block")
	}
	if !strings.HasSuffix(got, personaTestAgents) {
		t.Error("pre-existing content lost on update")
	}
}

// (c) SOUL.md removed → block removed, owner content intact.
func TestPersonaInline_SoulRemovedStripsBlock(t *testing.T) {
	dir := setupPersonaWorkspace(t)
	if _, err := ensurePersonaInlineBlockIn(dir); err != nil {
		t.Fatalf("seed call: %v", err)
	}

	if err := os.Remove(filepath.Join(dir, "SOUL.md")); err != nil {
		t.Fatalf("remove SOUL.md: %v", err)
	}
	modified, err := ensurePersonaInlineBlockIn(dir)
	if err != nil {
		t.Fatalf("strip call: %v", err)
	}
	if !modified {
		t.Fatal("expected modified=true when SOUL.md disappears")
	}

	got := readWS(t, dir, "AGENTS.md")
	if strings.Contains(got, personaInlineStart) || strings.Contains(got, personaInlineEnd) {
		t.Errorf("persona markers still present:\n%s", got)
	}
	if got != personaTestAgents {
		t.Errorf("original AGENTS.md content not restored:\ngot:  %q\nwant: %q", got, personaTestAgents)
	}

	modified, err = ensurePersonaInlineBlockIn(dir)
	if err != nil {
		t.Fatalf("post-strip call: %v", err)
	}
	if modified {
		t.Error("post-strip call: expected modified=false")
	}
}
