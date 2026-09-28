package hermes

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// legacySoulRuleBlock is how the OS rule block looked on devices that ran an
// os-server from before it moved to AGENTS.md: same body, inside SOUL.md, under
// the shared marker.
func legacySoulRuleBlock() string {
	return soulOSMarker + "\n" + soulSkillPrioritySentinel + " old wording\n---\n"
}

// ownMarkerSoulRuleBlock is the intermediate shape: still in SOUL.md, but under
// the Hermes-only marker.
func ownMarkerSoulRuleBlock() string {
	return soulSkillPriorityMarker + "\n" + soulSkillPrioritySentinel + " old wording\n---\n"
}

const testPersona = "# Lamp\n\n## Skill-driven turns (Non-Negotiable)\n- `[sensing:*]` → `skills/sensing/SKILL.md`."

// --- AGENTS.md: the OS rule block ------------------------------------------

func TestUpsertAgentsMDBlock_SeedsEmptyFile(t *testing.T) {
	got := upsertAgentsMDBlock("")
	if !strings.HasPrefix(got, soulOSMarker) {
		t.Fatalf("block missing from a fresh AGENTS.md:\n%q", got)
	}
	if !strings.Contains(got, soulSkillPrioritySentinel) {
		t.Fatalf("skill-priority rule missing:\n%q", got)
	}
	if !strings.Contains(got, "`connectors` skill") {
		t.Fatalf("connectors rule missing:\n%q", got)
	}
	if !strings.HasSuffix(got, "---\n") {
		t.Fatalf("block must close with --- separator:\n%q", got)
	}
	if upsertAgentsMDBlock(got) != got {
		t.Fatal("not idempotent on a freshly seeded file")
	}
}

func TestUpsertAgentsMDBlock_KeepsOwnerContent(t *testing.T) {
	got := upsertAgentsMDBlock("## Notes\n\nowner notes\n")
	if !strings.HasPrefix(got, soulOSMarker) {
		t.Fatalf("block must sit at the top:\n%q", got)
	}
	if !strings.Contains(got, "owner notes") {
		t.Fatalf("owner content lost:\n%q", got)
	}
}

// An OTA ships new wording: replace in place, never stack a second copy.
func TestUpsertAgentsMDBlock_RefreshesWithoutDuplicating(t *testing.T) {
	stale := soulOSMarker + "\n" + soulSkillPrioritySentinel + " old wording\n---\n\nowner notes\n"
	got := upsertAgentsMDBlock(stale)

	if strings.Contains(got, "old wording") {
		t.Fatalf("stale block survived:\n%q", got)
	}
	if n := strings.Count(got, soulOSMarker); n != 1 {
		t.Fatalf("want exactly one block, got %d:\n%q", n, got)
	}
	if !strings.Contains(got, "owner notes") {
		t.Fatalf("owner content lost:\n%q", got)
	}
}

// --- SOUL.md: the rule block must be cleaned out of it ----------------------

// The regression this split exists for: a persona block wears the same marker as
// the rule block once did, so the prune must never mistake one for the other.
func TestStripSoulOSRuleBlock_KeepsMarkedPersona(t *testing.T) {
	soul := soulOSMarker + "\n" + testPersona + "\n---\n\nowner notes\n"
	got := stripSoulOSRuleBlock(soul)

	for _, want := range []string{"Skill-driven turns", "`[sensing:*]`", "owner notes"} {
		if !strings.Contains(got, want) {
			t.Errorf("%q deleted from the persona:\n%q", want, got)
		}
	}
	if !strings.HasPrefix(got, soulOSMarker) {
		t.Errorf("persona block must stay at the top:\n%q", got)
	}
}

func TestStripSoulOSRuleBlock_RemovesBothShapes(t *testing.T) {
	persona := soulOSMarker + "\n" + testPersona + "\n---\n\nowner notes\n\n"
	for name, soul := range map[string]string{
		"own marker": persona + ownMarkerSoulRuleBlock(),
		"legacy":     persona + legacySoulRuleBlock(),
	} {
		got := stripSoulOSRuleBlock(soul)
		if strings.Contains(got, "old wording") {
			t.Errorf("%s: rule block survived in SOUL.md:\n%q", name, got)
		}
		if !strings.Contains(got, "owner notes") {
			t.Errorf("%s: owner content lost:\n%q", name, got)
		}
		if !strings.Contains(got, "Skill-driven turns") {
			t.Errorf("%s: persona lost:\n%q", name, got)
		}
		if stripSoulOSRuleBlock(got) != got {
			t.Errorf("%s: not idempotent", name)
		}
	}
}

// --- persona block ----------------------------------------------------------

func TestUpsertSoulPersonaBlock_SeedsEmptySoul(t *testing.T) {
	got := upsertSoulPersonaBlock("", testPersona)
	if !strings.HasPrefix(got, soulOSMarker+"\n# Lamp") {
		t.Fatalf("persona block missing from a fresh soul:\n%q", got)
	}
	if !strings.Contains(got, "\n---\n") {
		t.Fatalf("block must close with --- separator:\n%q", got)
	}
	if !strings.Contains(got, soulPersonalHeading) {
		t.Fatalf("owner-editable section missing:\n%q", got)
	}
	if upsertSoulPersonaBlock(got, testPersona) != got {
		t.Fatal("not idempotent on a freshly seeded soul")
	}
}

func TestUpsertSoulPersonaBlock_KeepsOwnerContent(t *testing.T) {
	got := upsertSoulPersonaBlock("## Personal\n\nowner notes\n", testPersona)
	if !strings.HasPrefix(got, soulOSMarker) {
		t.Fatalf("persona must sit at the top:\n%q", got)
	}
	if !strings.Contains(got, "owner notes") {
		t.Fatalf("owner content lost:\n%q", got)
	}
}

func TestUpsertSoulPersonaBlock_RefreshesWithoutDuplicating(t *testing.T) {
	first := upsertSoulPersonaBlock("", testPersona)
	got := upsertSoulPersonaBlock(first, "# Lamp v2\n\nnew wording")

	if strings.Contains(got, "Skill-driven turns") {
		t.Fatalf("stale persona survived:\n%q", got)
	}
	if n := strings.Count(got, soulOSMarker); n != 1 {
		t.Fatalf("want exactly one persona block, got %d:\n%q", n, got)
	}
	if !strings.Contains(got, "new wording") {
		t.Fatalf("new persona missing:\n%q", got)
	}
}

func TestUpsertSoulPersonaBlock_DropsManagedDefaultSoul(t *testing.T) {
	for name, stale := range map[string]string{
		"hermes fallback":  hermesSoulFallback,
		"openclaw seed":    "# Soul\n\nold template\n",
		"openclaw gateway": "# SOUL.md - Who You Are\n\nold template\n",
		// What presync leaves behind on a freshly flashed device: the Hermes
		// gateway re-seeds its own persona whenever SOUL.md is missing, and it
		// opens with prose rather than a heading.
		"hermes gateway seed": "You are Hermes Agent, built by Nous Research. Be direct.\n\nold template\n",
	} {
		got := upsertSoulPersonaBlock(stale, testPersona)
		if strings.Contains(got, "old template") || strings.Contains(got, "Hermes Agent Persona") ||
			strings.Contains(got, "Nous Research") {
			t.Errorf("%s: managed default kept as owner content:\n%q", name, got)
		}
		if !strings.Contains(got, soulPersonalHeading) {
			t.Errorf("%s: owner-editable section missing:\n%q", name, got)
		}
	}
}

func TestUpsertSoulPersonaBlock_KeepsPersonalSectionUnderDefault(t *testing.T) {
	stale := "# Soul\n\nold template\n\n" + soulPersonalHeading + "\n\nI drink tea at 3pm.\n"
	got := upsertSoulPersonaBlock(stale, testPersona)

	if strings.Contains(got, "old template") {
		t.Fatalf("managed default survived:\n%q", got)
	}
	if !strings.Contains(got, "I drink tea at 3pm.") {
		t.Fatalf("owner edits lost:\n%q", got)
	}
}

// --- both files together, as EnsureOnboarding drives them -------------------

// Every boot runs the persona upsert, the SOUL prune and the AGENTS.md upsert.
// Both files must settle after the first pass and never grow again.
func TestPromptFiles_StableAcrossBoots(t *testing.T) {
	boot := func(soul, agents string) (string, string) {
		return stripSoulOSRuleBlock(upsertSoulPersonaBlock(soul, testPersona)), upsertAgentsMDBlock(agents)
	}

	soul1, agents1 := boot("", "")
	soul2, agents2 := boot(soul1, agents1)
	if soul1 != soul2 {
		t.Fatalf("second boot rewrote SOUL.md:\n first=%q\nsecond=%q", soul1, soul2)
	}
	if agents1 != agents2 {
		t.Fatalf("second boot rewrote AGENTS.md:\n first=%q\nsecond=%q", agents1, agents2)
	}
	if !strings.Contains(soul2, "`[sensing:*]`") {
		t.Fatalf("sensing routing rule missing from SOUL.md:\n%q", soul2)
	}
	if strings.Contains(soul2, soulSkillPrioritySentinel) {
		t.Fatalf("rule block leaked into SOUL.md:\n%q", soul2)
	}
	if !strings.Contains(agents2, soulSkillPrioritySentinel) {
		t.Fatalf("rule block missing from AGENTS.md:\n%q", agents2)
	}
}

// A device updating from an older os-server carries the rule block inside
// SOUL.md. One boot must move it out and leave everything else intact.
func TestPromptFiles_MigratesRuleBlockOutOfSoul(t *testing.T) {
	soul := soulOSMarker + "\n" + testPersona + "\n---\n\n## Personal\n\nowner notes\n\n" +
		"## Your identity card\n\n- **Name:** Ngan\n\n" + legacySoulRuleBlock()

	gotSoul := stripSoulOSRuleBlock(upsertSoulPersonaBlock(soul, testPersona))
	gotAgents := upsertAgentsMDBlock("")

	if strings.Contains(gotSoul, "old wording") {
		t.Errorf("rule block still in SOUL.md:\n%q", gotSoul)
	}
	for _, want := range []string{"Skill-driven turns", "owner notes", "- **Name:** Ngan"} {
		if !strings.Contains(gotSoul, want) {
			t.Errorf("%q lost from SOUL.md:\n%q", want, gotSoul)
		}
	}
	if !strings.Contains(gotAgents, soulSkillPrioritySentinel) {
		t.Errorf("rule block missing from AGENTS.md:\n%q", gotAgents)
	}
}

func mkSkill(t *testing.T, dir, name string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, name), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, name, "SKILL.md"), []byte("# "+name), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestPruneImportedDuplicates_RemovesWhenBaseExists(t *testing.T) {
	dir := t.TempDir()
	mkSkill(t, dir, "connectors")
	mkSkill(t, dir, "connectors-imported")
	mkSkill(t, dir, "voice")

	if got := pruneImportedDuplicatesIn(dir); got != 1 {
		t.Fatalf("changed = %d, want 1", got)
	}
	if _, err := os.Stat(filepath.Join(dir, "connectors-imported")); !os.IsNotExist(err) {
		t.Fatalf("duplicate not removed")
	}
	if _, err := os.Stat(filepath.Join(dir, "connectors", "SKILL.md")); err != nil {
		t.Fatalf("canonical copy lost: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "voice")); err != nil {
		t.Fatalf("unrelated skill touched: %v", err)
	}
}

func TestPruneImportedDuplicates_RenamesWhenOnlyImportedExists(t *testing.T) {
	dir := t.TempDir()
	mkSkill(t, dir, "standup-imported")

	if got := pruneImportedDuplicatesIn(dir); got != 1 {
		t.Fatalf("changed = %d, want 1", got)
	}
	if _, err := os.Stat(filepath.Join(dir, "standup", "SKILL.md")); err != nil {
		t.Fatalf("imported-only skill not renamed to canonical name: %v", err)
	}
}

func TestPruneImportedDuplicates_NoopCases(t *testing.T) {
	dir := t.TempDir()
	mkSkill(t, dir, "connectors")
	// plain file with the suffix must be ignored (not a skill dir)
	if err := os.WriteFile(filepath.Join(dir, "notes-imported"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := pruneImportedDuplicatesIn(dir); got != 0 {
		t.Fatalf("changed = %d, want 0", got)
	}
	if got := pruneImportedDuplicatesIn(filepath.Join(dir, "absent")); got != 0 {
		t.Fatalf("absent dir: changed = %d, want 0", got)
	}
}
