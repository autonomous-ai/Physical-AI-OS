package migratepersona

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestBuildIdentityBlock_FilledFieldsOnly(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "IDENTITY.md")
	content := "# IDENTITY.md - Who Am I?\n\n_Fill this in._\n\n" +
		"- **Name:** Ngân\n" +
		"- **Creature:**\n  _(AI? robot?)_\n" +
		"- **Vibe:**\n  _(warm? calm?)_\n"
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	got := buildIdentityBlock(p)
	if !strings.Contains(got, identityCardHeading) {
		t.Fatalf("missing heading: %q", got)
	}
	if !strings.Contains(got, "- **Name:** Ngân") {
		t.Fatalf("missing name: %q", got)
	}
	if strings.Contains(got, "Creature") || strings.Contains(got, "Vibe") || strings.Contains(got, "_(") {
		t.Fatalf("unfilled placeholder leaked: %q", got)
	}
}

// A switch must carry the source runtime's soul into the target even when the
// target already has a SOUL.md (here a factory-reset stub): copyPersona overwrites
// (Overwrite=true), then the identity card is inlined on top.
func TestMigrate_OverwritesTargetSoulOnSwitch(t *testing.T) {
	cfgDir := t.TempDir()
	hermesRoot := t.TempDir()
	ws := filepath.Join(cfgDir, "workspace")
	if err := os.MkdirAll(ws, 0o755); err != nil {
		t.Fatal(err)
	}
	const lampSoul = "# Soul\n\nYou are **Lamp** — a living being on the desk.\n"
	if err := os.WriteFile(filepath.Join(ws, "SOUL.md"), []byte(lampSoul), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(ws, "IDENTITY.md"), []byte("- **Name:** Hà\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	// Target already holds the factory-reset stub — the case that used to
	// conflict-skip and lose the persona.
	if err := os.WriteFile(filepath.Join(hermesRoot, "SOUL.md"), []byte("# Hermes Agent Persona\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	opts := DefaultOptions(cfgDir, hermesRoot)
	opts.Execute = true
	opts.Overwrite = true // mirrors ProvidePersonaMigration (a switch is explicit)

	if _, err := Run(OpenclawToHermes, opts); err != nil {
		t.Fatalf("Run: %v", err)
	}

	got, err := os.ReadFile(filepath.Join(hermesRoot, "SOUL.md"))
	if err != nil {
		t.Fatal(err)
	}
	soul := string(got)
	if strings.Contains(soul, "# Hermes Agent Persona") {
		t.Errorf("stub not overwritten — soul body did not migrate:\n%s", soul)
	}
	if !strings.Contains(soul, "You are **Lamp**") {
		t.Errorf("openclaw soul body missing after switch:\n%s", soul)
	}
	if !strings.Contains(soul, identityCardHeading) || !strings.Contains(soul, "- **Name:** Hà") {
		t.Errorf("identity card not inlined on top of migrated soul:\n%s", soul)
	}
}

// The reverse switch (hermes→openclaw) must NOT carry the Hermes-only identity
// card back into the OpenClaw SOUL — OpenClaw owns the name via IDENTITY.md.
func TestMigrate_StripsIdentityCardOnReverseSwitch(t *testing.T) {
	cfgDir := t.TempDir()
	hermesRoot := t.TempDir()
	ws := filepath.Join(cfgDir, "workspace")
	if err := os.MkdirAll(ws, 0o755); err != nil {
		t.Fatal(err)
	}
	// Hermes soul = real soul body + the inlined identity card (trailing).
	hermesSoul := "# Soul\n\nYou are **Lamp**.\n\n" + identityCardHeading +
		"\n\nYour owner set this — it overrides any default name or vibe above.\n\n- **Name:** Ngân\n"
	if err := os.WriteFile(filepath.Join(hermesRoot, "SOUL.md"), []byte(hermesSoul), 0o644); err != nil {
		t.Fatal(err)
	}

	opts := DefaultOptions(cfgDir, hermesRoot)
	opts.Execute = true
	opts.Overwrite = true

	if _, err := Run(HermesToOpenclaw, opts); err != nil {
		t.Fatalf("Run: %v", err)
	}

	got, err := os.ReadFile(filepath.Join(ws, "SOUL.md"))
	if err != nil {
		t.Fatal(err)
	}
	soul := string(got)
	if strings.Contains(soul, identityCardHeading) || strings.Contains(soul, "Ngân") {
		t.Errorf("identity card leaked back into openclaw soul:\n%s", soul)
	}
	if !strings.Contains(soul, "You are **Lamp**") {
		t.Errorf("soul body lost on reverse switch:\n%s", soul)
	}
}

// KNOWLEDGE.md (the agent's distilled learnings) must be folded into the Hermes
// MEMORY.md — Hermes reads only MEMORY.md/USER.md by name, so a separate file
// would be ignored. Its section headings survive as entry prefixes, and its
// `<!-- ... -->` template placeholders must NOT leak in as memory entries.
func TestMigrate_FoldsKnowledgeIntoMemory(t *testing.T) {
	cfgDir := t.TempDir()
	hermesRoot := t.TempDir()
	ws := filepath.Join(cfgDir, "workspace")
	if err := os.MkdirAll(ws, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(ws, "MEMORY.md"), []byte("- Owner sleeps late on weekends.\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	knowledge := "# KNOWLEDGE.md — Accumulated Learnings\n\n## Hardware\n\n" +
		"<!-- Lessons about the body, quirks, limits. -->\n\n" +
		"- Servo elbow jitters above 60 degrees.\n\n" +
		"## Mistakes Made\n\n- Said NO_REPLY to a direct question once.\n"
	if err := os.WriteFile(filepath.Join(ws, "KNOWLEDGE.md"), []byte(knowledge), 0o644); err != nil {
		t.Fatal(err)
	}

	opts := DefaultOptions(cfgDir, hermesRoot)
	opts.Execute = true
	if _, err := Run(OpenclawToHermes, opts); err != nil {
		t.Fatalf("Run: %v", err)
	}

	got, err := os.ReadFile(filepath.Join(hermesRoot, "memories", "MEMORY.md"))
	if err != nil {
		t.Fatal(err)
	}
	mem := string(got)
	if !strings.Contains(mem, "Servo elbow jitters above 60 degrees.") {
		t.Errorf("knowledge entry not folded into MEMORY.md:\n%s", mem)
	}
	if !strings.Contains(mem, "Hardware:") {
		t.Errorf("section heading not preserved as entry prefix:\n%s", mem)
	}
	if !strings.Contains(mem, "Owner sleeps late") {
		t.Errorf("original MEMORY.md content lost:\n%s", mem)
	}
	if strings.Contains(mem, "Lessons about the body") {
		t.Errorf("HTML comment placeholder leaked into memory:\n%s", mem)
	}
}

// The reverse switch must RESTORE the owner's name into workspace/IDENTITY.md
// (the file OpenClaw reads for wake words) — the name set under Hermes lives only
// in the SOUL identity card, which is stripped from the OpenClaw SOUL. Restoring
// into an existing template must replace the placeholder line and keep the rest.
func TestMigrate_RestoresIdentityNameOnReverseSwitch(t *testing.T) {
	cfgDir := t.TempDir()
	hermesRoot := t.TempDir()
	ws := filepath.Join(cfgDir, "workspace")
	if err := os.MkdirAll(ws, 0o755); err != nil {
		t.Fatal(err)
	}
	hermesSoul := "# Soul\n\nYou are **Lamp**.\n\n" + identityCardHeading +
		"\n\nYour owner set this — it overrides any default name or vibe above.\n\n- **Name:** Ngân\n"
	if err := os.WriteFile(filepath.Join(hermesRoot, "SOUL.md"), []byte(hermesSoul), 0o644); err != nil {
		t.Fatal(err)
	}
	// OpenClaw template with an unfilled Name placeholder + an unrelated slot.
	tmpl := "# IDENTITY.md - Who Am I?\n\n- **Name:**\n  _(pick something you like)_\n- **Vibe:** calm\n"
	if err := os.WriteFile(filepath.Join(ws, "IDENTITY.md"), []byte(tmpl), 0o644); err != nil {
		t.Fatal(err)
	}

	opts := DefaultOptions(cfgDir, hermesRoot)
	opts.Execute = true
	opts.Overwrite = true
	if _, err := Run(HermesToOpenclaw, opts); err != nil {
		t.Fatalf("Run: %v", err)
	}

	got, err := os.ReadFile(filepath.Join(ws, "IDENTITY.md"))
	if err != nil {
		t.Fatal(err)
	}
	id := string(got)
	if !strings.Contains(id, "- **Name:** Ngân") {
		t.Errorf("name not restored into IDENTITY.md:\n%s", id)
	}
	if strings.Contains(id, "pick something you like") {
		t.Errorf("stale placeholder hint not dropped:\n%s", id)
	}
	if !strings.Contains(id, "- **Vibe:** calm") {
		t.Errorf("unrelated template slot lost:\n%s", id)
	}
}

// When workspace/IDENTITY.md does not exist yet (first switch, before OpenClaw's
// own onboard creates it), the reverse restore must create it with the name.
func TestMigrate_CreatesIdentityWhenAbsentOnReverseSwitch(t *testing.T) {
	cfgDir := t.TempDir()
	hermesRoot := t.TempDir()
	ws := filepath.Join(cfgDir, "workspace")
	if err := os.MkdirAll(ws, 0o755); err != nil {
		t.Fatal(err)
	}
	hermesSoul := "# Soul\n\nYou are **Lamp**.\n\n" + identityCardHeading +
		"\n\nYour owner set this.\n\n- **Name:** Ngân\n"
	if err := os.WriteFile(filepath.Join(hermesRoot, "SOUL.md"), []byte(hermesSoul), 0o644); err != nil {
		t.Fatal(err)
	}

	opts := DefaultOptions(cfgDir, hermesRoot)
	opts.Execute = true
	opts.Overwrite = true
	if _, err := Run(HermesToOpenclaw, opts); err != nil {
		t.Fatalf("Run: %v", err)
	}

	got, err := os.ReadFile(filepath.Join(ws, "IDENTITY.md"))
	if err != nil {
		t.Fatalf("IDENTITY.md not created: %v", err)
	}
	if !strings.Contains(string(got), "- **Name:** Ngân") {
		t.Errorf("name not written to created IDENTITY.md:\n%s", string(got))
	}
}

// Hub round-trip: openclaw → hermes → openclaw on one device must preserve the
// owner's name (restored to IDENTITY.md), the soul body, and memory content
// (folded into Hermes MEMORY then carried back). Exercises the runtime-keyed
// RunMigration entry point both ways.
func TestRoundTrip_OpenclawHermesOpenclaw_PreservesContent(t *testing.T) {
	cfgDir := t.TempDir()
	hermesRoot := t.TempDir()
	ws := filepath.Join(cfgDir, "workspace")
	if err := os.MkdirAll(ws, 0o755); err != nil {
		t.Fatal(err)
	}
	write := func(p, s string) {
		if err := os.WriteFile(filepath.Join(ws, p), []byte(s), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("SOUL.md", "# Soul\n\nYou are **Lamp** — a living being on the desk.\n")
	write("IDENTITY.md", "# IDENTITY.md\n\n- **Name:** Ngân\n")
	write("MEMORY.md", "- Owner sleeps late on weekends.\n")
	write("KNOWLEDGE.md", "# KNOWLEDGE.md\n\n## Hardware\n\n- Servo elbow jitters above 60 degrees.\n")

	opts := DefaultOptions(cfgDir, hermesRoot)
	opts.Execute = true
	opts.Overwrite = true

	if _, err := RunMigration(RuntimeOpenclaw, RuntimeHermes, opts); err != nil {
		t.Fatalf("forward: %v", err)
	}
	if _, err := RunMigration(RuntimeHermes, RuntimeOpenclaw, opts); err != nil {
		t.Fatalf("reverse: %v", err)
	}

	id, _ := os.ReadFile(filepath.Join(ws, "IDENTITY.md"))
	if !strings.Contains(string(id), "- **Name:** Ngân") {
		t.Errorf("name lost on round-trip:\n%s", id)
	}
	soul, _ := os.ReadFile(filepath.Join(ws, "SOUL.md"))
	if !strings.Contains(string(soul), "You are **Lamp**") {
		t.Errorf("soul body lost on round-trip:\n%s", soul)
	}
	if strings.Contains(string(soul), identityCardHeading) {
		t.Errorf("identity card leaked into openclaw soul:\n%s", soul)
	}
	mem, _ := os.ReadFile(filepath.Join(ws, "MEMORY.md"))
	if !strings.Contains(string(mem), "Servo elbow jitters above 60 degrees.") {
		t.Errorf("knowledge content lost on round-trip:\n%s", mem)
	}
	if !strings.Contains(string(mem), "Owner sleeps late") {
		t.Errorf("memory content lost on round-trip:\n%s", mem)
	}
}

func TestBuildIdentityBlock_MissingFile(t *testing.T) {
	if got := buildIdentityBlock(filepath.Join(t.TempDir(), "nope.md")); got != "" {
		t.Fatalf("expected empty for missing file, got %q", got)
	}
}

func TestBuildIdentityBlock_NoFilledFields(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "IDENTITY.md")
	if err := os.WriteFile(p, []byte("- **Name:**\n  _(pick one)_\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := buildIdentityBlock(p); got != "" {
		t.Fatalf("expected empty when no filled fields, got %q", got)
	}
}

// Hermes keeps its skill-priority block under a Hermes-only marker, so the
// destination runtime's own strip (keyed on `<!-- OS DO NOT REMOVE -->`) cannot
// clear it. Migrating out must drop it here, or those Hermes-specific rules ride
// into the next runtime's SOUL.md and stay there.
func TestHermesRead_DropsOSManagedSkillBlock(t *testing.T) {
	root := t.TempDir()
	soul := "<!-- OS DO NOT REMOVE -->\n# Lamp persona\n---\n\n" +
		"## Personal\n\nowner notes\n\n" +
		hermesOSBlockMarker + "\n" +
		"**Skill priority (MANDATORY):** device skills beat bundled ones\n" +
		"**Silence = the literal token `NO_REPLY`.**\n---\n"
	if err := os.WriteFile(filepath.Join(root, "SOUL.md"), []byte(soul), 0o644); err != nil {
		t.Fatal(err)
	}

	b, err := hermesAdapter{}.read(Options{HermesRoot: root})
	if err != nil {
		t.Fatalf("read: %v", err)
	}

	if strings.Contains(b.Soul, "Skill priority (MANDATORY)") {
		t.Errorf("OS-managed block leaked into the bundle:\n%s", b.Soul)
	}
	if strings.Contains(b.Soul, hermesOSBlockMarker) {
		t.Errorf("marker left behind:\n%s", b.Soul)
	}
	for _, want := range []string{"# Lamp persona", "owner notes"} {
		if !strings.Contains(b.Soul, want) {
			t.Errorf("%q lost from the persona:\n%s", want, b.Soul)
		}
	}
}
