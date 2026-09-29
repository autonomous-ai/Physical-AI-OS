package skills

import (
	"archive/zip"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRenderSkillMarkdown(t *testing.T) {
	got := RenderSkillMarkdown("weekly-status-report",
		"Summarise the week's activity.", "1. Collect\n2. Group\n")

	want := "---\nname: weekly-status-report\ndescription: Summarise the week's activity.\n---\n\n1. Collect\n2. Group\n"
	if got != want {
		t.Fatalf("rendered SKILL.md mismatch:\ngot:\n%q\nwant:\n%q", got, want)
	}
}

// A newline in the description must be flattened.
func TestRenderSkillMarkdownFlattensDescription(t *testing.T) {
	got := RenderSkillMarkdown("x", "line one\nline two\t  spaced", "body")

	if !strings.Contains(got, "description: line one line two spaced\n") {
		t.Fatalf("description not flattened to one line:\n%s", got)
	}
	if n := strings.Count(got, "---\n"); n != 2 {
		t.Errorf("front-matter delimiters = %d, want 2:\n%s", n, got)
	}
}

func TestWriteAuthoredSkill(t *testing.T) {
	dir := t.TempDir()

	path, err := WriteAuthoredSkill(dir, "my-skill", "Does a thing.", "Do the thing.")
	if err != nil {
		t.Fatalf("write: %v", err)
	}
	if want := filepath.Join(dir, "my-skill", "SKILL.md"); path != want {
		t.Errorf("path = %q, want %q", path, want)
	}

	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if !strings.HasPrefix(string(content), "---\nname: my-skill\n") {
		t.Errorf("unexpected content:\n%s", content)
	}
}

func TestWriteAuthoredSkillRejectsBadInput(t *testing.T) {
	cases := []struct {
		label                           string
		name, description, instructions string
		wantErr                         error
	}{
		{"empty name", "", "d", "i", ErrInvalidSkillName},
		{"uppercase", "MySkill", "d", "i", ErrInvalidSkillName},
		{"traversal", "../evil", "d", "i", ErrInvalidSkillName},
		{"slash", "a/b", "d", "i", ErrInvalidSkillName},
		{"too long", strings.Repeat("a", 65), "d", "i", ErrInvalidSkillName},
	}
	for _, tc := range cases {
		t.Run(tc.label, func(t *testing.T) {
			dir := t.TempDir()
			if _, err := WriteAuthoredSkill(dir, tc.name, tc.description, tc.instructions); !errors.Is(err, tc.wantErr) {
				t.Fatalf("err = %v, want %v", err, tc.wantErr)
			}
			entries, _ := os.ReadDir(dir)
			if len(entries) != 0 {
				t.Errorf("nothing should have been written, found %d entries", len(entries))
			}
		})
	}

	dir := t.TempDir()
	if _, err := WriteAuthoredSkill(dir, "ok", "  ", "i"); err == nil {
		t.Error("blank description must be rejected")
	}
	if _, err := WriteAuthoredSkill(dir, "ok", "d", "\n\t "); err == nil {
		t.Error("blank instructions must be rejected")
	}
}

// Authoring must never clobber a store- or OTA-installed skill of the same name.
func TestWriteAuthoredSkillRefusesToOverwrite(t *testing.T) {
	dir := t.TempDir()
	existing := filepath.Join(dir, "music", "SKILL.md")
	if err := os.MkdirAll(filepath.Dir(existing), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(existing, []byte("ORIGINAL"), 0644); err != nil {
		t.Fatal(err)
	}

	if _, err := WriteAuthoredSkill(dir, "music", "d", "i"); !errors.Is(err, ErrSkillExists) {
		t.Fatalf("err = %v, want ErrSkillExists", err)
	}
	content, _ := os.ReadFile(existing)
	if string(content) != "ORIGINAL" {
		t.Errorf("existing skill was modified: %q", content)
	}
}

func makeZip(t *testing.T, path string, entries map[string]string) {
	t.Helper()
	f, err := os.Create(path)
	if err != nil {
		t.Fatalf("create zip: %v", err)
	}
	defer f.Close()
	w := zip.NewWriter(f)
	for name, content := range entries {
		e, err := w.Create(name)
		if err != nil {
			t.Fatalf("entry %s: %v", name, err)
		}
		if _, err := e.Write([]byte(content)); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatalf("close zip: %v", err)
	}
}

// A single wrapping <name>/ dir names the skill and is stripped.
func TestInstallSkillArchiveStripsWrappingDir(t *testing.T) {
	tmp := t.TempDir()
	archive := filepath.Join(tmp, "a.zip")
	makeZip(t, archive, map[string]string{
		"design-critique/SKILL.md":            "# Design Critique\n",
		"design-critique/reference/notes.md":  "notes",
		"design-critique/.DS_Store":           "junk",
		"__MACOSX/design-critique/._SKILL.md": "junk",
	})
	skillsDir := filepath.Join(tmp, "skills")

	dir, count, err := InstallSkillArchive(archive, skillsDir, "fallback")
	if err != nil {
		t.Fatalf("install: %v", err)
	}
	if want := filepath.Join(skillsDir, "design-critique"); dir != want {
		t.Fatalf("dir = %q, want %q", dir, want)
	}
	if count != 2 {
		t.Errorf("count = %d, want 2 (cruft filtered)", count)
	}
	if _, err := os.Stat(filepath.Join(dir, "SKILL.md")); err != nil {
		t.Errorf("SKILL.md not at the skill root — wrapping dir was not stripped: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "reference", "notes.md")); err != nil {
		t.Errorf("nested file missing: %v", err)
	}
	if _, err := os.Stat(dir + ".new"); !os.IsNotExist(err) {
		t.Error("staging dir was left behind")
	}
}

// OTA-style archives put files at the root, so fallbackName names the skill.
func TestInstallSkillArchiveUsesFallbackName(t *testing.T) {
	tmp := t.TempDir()
	archive := filepath.Join(tmp, "a.zip")
	makeZip(t, archive, map[string]string{"SKILL.md": "body"})
	skillsDir := filepath.Join(tmp, "skills")

	dir, _, err := InstallSkillArchive(archive, skillsDir, "my-skill")
	if err != nil {
		t.Fatalf("install: %v", err)
	}
	if want := filepath.Join(skillsDir, "my-skill"); dir != want {
		t.Fatalf("dir = %q, want %q", dir, want)
	}
}

// Unlike authoring, installing from the store deliberately replaces.
func TestInstallSkillArchiveReplacesExisting(t *testing.T) {
	tmp := t.TempDir()
	skillsDir := filepath.Join(tmp, "skills")
	stale := filepath.Join(skillsDir, "x", "OLD.md")
	if err := os.MkdirAll(filepath.Dir(stale), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(stale, []byte("old"), 0644); err != nil {
		t.Fatal(err)
	}

	archive := filepath.Join(tmp, "a.zip")
	makeZip(t, archive, map[string]string{"x/SKILL.md": "new"})

	dir, _, err := InstallSkillArchive(archive, skillsDir, "")
	if err != nil {
		t.Fatalf("install: %v", err)
	}
	if _, err := os.Stat(stale); !os.IsNotExist(err) {
		t.Error("stale file survived the replace — install is not a clean swap")
	}
	if _, err := os.Stat(filepath.Join(dir, "SKILL.md")); err != nil {
		t.Errorf("new content missing: %v", err)
	}
	if _, err := os.Stat(dir + ".old"); !os.IsNotExist(err) {
		t.Error("backup dir was left behind")
	}
}

func TestInstallSkillArchiveRejectsTraversal(t *testing.T) {
	tmp := t.TempDir()
	archive := filepath.Join(tmp, "evil.zip")
	// Two top-level segments so the per-entry guard must catch "..".
	makeZip(t, archive, map[string]string{
		"SKILL.md":      "ok",
		"../escaped.md": "pwned",
	})
	skillsDir := filepath.Join(tmp, "skills")

	if _, _, err := InstallSkillArchive(archive, skillsDir, "x"); err == nil {
		t.Fatal("expected traversal entry to be rejected")
	}
	if _, err := os.Stat(filepath.Join(tmp, "escaped.md")); !os.IsNotExist(err) {
		t.Error("traversal entry escaped the skills dir")
	}
	if _, err := os.Stat(filepath.Join(skillsDir, "x")); !os.IsNotExist(err) {
		t.Error("a failed install must leave nothing behind")
	}
}

func TestInstallSkillArchiveEmpty(t *testing.T) {
	tmp := t.TempDir()
	archive := filepath.Join(tmp, "empty.zip")
	makeZip(t, archive, map[string]string{})

	if _, _, err := InstallSkillArchive(archive, filepath.Join(tmp, "skills"), "x"); !errors.Is(err, ErrEmptyArchive) {
		t.Fatalf("err = %v, want ErrEmptyArchive", err)
	}
}

func TestDeleteSkill(t *testing.T) {
	dir := t.TempDir()
	seedSkill(t, dir, "music", map[string]string{
		"SKILL.md":           "body",
		"reference/notes.md": "notes",
	})
	seedSkill(t, dir, "voice", map[string]string{"SKILL.md": "body"})

	path, err := DeleteSkill(dir, "music")
	if err != nil {
		t.Fatalf("delete: %v", err)
	}
	if want := filepath.Join(dir, "music"); path != want {
		t.Errorf("path = %q, want %q", path, want)
	}
	if _, err := os.Stat(filepath.Join(dir, "music")); !os.IsNotExist(err) {
		t.Error("skill dir survived the delete")
	}
	if _, err := os.Stat(filepath.Join(dir, "voice", "SKILL.md")); err != nil {
		t.Errorf("unrelated skill was affected: %v", err)
	}
}

// A missing skill is an error, not a silent success.
func TestDeleteSkillMissing(t *testing.T) {
	if _, err := DeleteSkill(t.TempDir(), "nope"); !errors.Is(err, ErrSkillNotFound) {
		t.Fatalf("err = %v, want ErrSkillNotFound", err)
	}
}

func TestDeleteSkillRejectsBadName(t *testing.T) {
	dir := t.TempDir()
	outside := filepath.Join(dir, "outside.md")
	if err := os.WriteFile(outside, []byte("keep"), 0644); err != nil {
		t.Fatal(err)
	}

	for _, name := range []string{"", "..", "../", "a/b", "Music", strings.Repeat("a", 65)} {
		if _, err := DeleteSkill(dir, name); !errors.Is(err, ErrInvalidSkillName) {
			t.Errorf("name %q: err = %v, want ErrInvalidSkillName", name, err)
		}
	}
	if _, err := os.Stat(outside); err != nil {
		t.Errorf("traversal reached outside the skills dir: %v", err)
	}
}

// A plain file where a skill dir should be is refused, not deleted.
func TestDeleteSkillRefusesNonDirectory(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "music"), []byte("not a skill"), 0644); err != nil {
		t.Fatal(err)
	}

	if _, err := DeleteSkill(dir, "music"); !errors.Is(err, ErrSkillNotFound) {
		t.Fatalf("err = %v, want ErrSkillNotFound", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "music")); err != nil {
		t.Error("the file was deleted despite not being a skill dir")
	}
}

// Hermes namespaces its skills dir, so the delete walks roots in order.
func TestDeleteSkillFrom(t *testing.T) {
	base := t.TempDir()
	authored := filepath.Join(base, "authored")
	imported := filepath.Join(base, "openclaw-imports")
	seedSkill(t, authored, "music", map[string]string{"SKILL.md": "MINE"})
	seedSkill(t, imported, "music", map[string]string{"SKILL.md": "IMPORTED"})
	seedSkill(t, imported, "voice", map[string]string{"SKILL.md": "VOICE"})

	if _, err := DeleteSkillFrom("music", authored, imported); err != nil {
		t.Fatalf("delete music: %v", err)
	}
	if _, err := os.Stat(filepath.Join(authored, "music")); !os.IsNotExist(err) {
		t.Error("device-owned copy should have been deleted")
	}
	if _, err := os.Stat(filepath.Join(imported, "music")); err != nil {
		t.Error("imported copy must be left alone when the first root had it")
	}

	if _, err := DeleteSkillFrom("voice", authored, imported); err != nil {
		t.Fatalf("delete voice: %v", err)
	}
	if _, err := os.Stat(filepath.Join(imported, "voice")); !os.IsNotExist(err) {
		t.Error("second root not searched")
	}

	if _, err := DeleteSkillFrom("absent", authored, imported); !errors.Is(err, ErrSkillNotFound) {
		t.Errorf("err = %v, want ErrSkillNotFound", err)
	}
}

// A bad name is fatal for every root — don't keep probing with it.
func TestDeleteSkillFromRejectsBadNameOnce(t *testing.T) {
	base := t.TempDir()
	if _, err := DeleteSkillFrom("../evil", filepath.Join(base, "a"), filepath.Join(base, "b")); !errors.Is(err, ErrInvalidSkillName) {
		t.Fatalf("err = %v, want ErrInvalidSkillName", err)
	}
}

func TestSlugifySkillName(t *testing.T) {
	cases := []struct{ in, want string }{
		{"weekly-status-report", "weekly-status-report"},
		{"My Skill", "my-skill"},
		{"design-critique.skill", "design-critique-skill"},
		{"  Padded  Name  ", "padded-name"},
		{"a___b", "a___b"},
		{"UPPER", "upper"},
		{"multi   spaces", "multi-spaces"},
		{"trailing---", "trailing"},
		{"---leading", "leading"},
		{"tiếng việt", "ti-ng-vi-t"},
		{"", ""},
		{"   ", ""},
		{"...", ""},
		{"日本語", ""},
	}
	for _, tc := range cases {
		if got := SlugifySkillName(tc.in); got != tc.want {
			t.Errorf("SlugifySkillName(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

// Slug output must pass the validator.
func TestSlugifySkillNameOutputIsValid(t *testing.T) {
	for _, in := range []string{
		"My Skill", "design-critique.skill", "UPPER", "a b c",
		strings.Repeat("x", 200), strings.Repeat("a b ", 40),
	} {
		got := SlugifySkillName(in)
		if got == "" {
			t.Errorf("input %q produced an empty slug", in)
			continue
		}
		if err := ValidateSkillName(got); err != nil {
			t.Errorf("SlugifySkillName(%q) = %q, which ValidateSkillName rejects: %v", in, got, err)
		}
	}
}

func TestParseSkillFrontMatter(t *testing.T) {
	name, desc, err := ParseSkillFrontMatter([]byte(
		"---\nname: weekly-report\ndescription: Sums up the week.\n---\n\nbody"))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if name != "weekly-report" || desc != "Sums up the week." {
		t.Fatalf("name=%q desc=%q", name, desc)
	}
}

// Unknown front-matter keys must not make a valid skill unreadable.
func TestParseSkillFrontMatterToleratesExtraKeys(t *testing.T) {
	name, desc, err := ParseSkillFrontMatter([]byte(
		"---\nname: algorithmic-art\ndescription: Creating algorithmic art with p5.js.\n" +
			"license: Complete terms in LICENSE.txt\nversion: 1.2.0\n---\n"))
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if name != "algorithmic-art" || desc != "Creating algorithmic art with p5.js." {
		t.Errorf("name=%q desc=%q", name, desc)
	}
}

// A `name:` nested under another key is NOT the skill's name.
func TestParseSkillFrontMatterIgnoresNestedKeys(t *testing.T) {
	_, _, err := ParseSkillFrontMatter([]byte(
		"---\ndescription: Has a nested name only.\nmetadata:\n  name: sneaky\n---\n"))
	if !errors.Is(err, ErrInvalidFrontMatter) {
		t.Fatalf("err = %v, want ErrInvalidFrontMatter (nested name must not count)", err)
	}
}

func TestParseSkillFrontMatterRejects(t *testing.T) {
	cases := []struct{ label, content string }{
		{"no front-matter", "# Just a heading\n\nbody"},
		{"unterminated but keyless", "---\n"},
		{"name only", "---\nname: x\n---\n"},
		{"description only", "---\ndescription: d\n---\n"},
		{"empty file", ""},
		{"content before block", "hello\n---\nname: x\ndescription: d\n---\n"},
	}
	for _, tc := range cases {
		t.Run(tc.label, func(t *testing.T) {
			if _, _, err := ParseSkillFrontMatter([]byte(tc.content)); !errors.Is(err, ErrInvalidFrontMatter) {
				t.Fatalf("err = %v, want ErrInvalidFrontMatter", err)
			}
		})
	}
}

// Round-trip: what RenderSkillMarkdown writes must parse back.
func TestFrontMatterRoundTrip(t *testing.T) {
	md := RenderSkillMarkdown("weekly-report", "Sums up the week.", "1. Collect")
	name, desc, err := ParseSkillFrontMatter([]byte(md))
	if err != nil {
		t.Fatalf("rendered SKILL.md does not parse back: %v", err)
	}
	if name != "weekly-report" || desc != "Sums up the week." {
		t.Errorf("name=%q desc=%q", name, desc)
	}
}

func TestInstallSkillMarkdown(t *testing.T) {
	dir := t.TempDir()
	content := []byte("---\nname: weekly-report\ndescription: Sums up the week.\n---\n\nbody")

	path, err := InstallSkillMarkdown(dir, content)
	if err != nil {
		t.Fatalf("install: %v", err)
	}
	if want := filepath.Join(dir, "weekly-report"); path != want {
		t.Fatalf("path = %q, want %q", path, want)
	}
	got, err := os.ReadFile(filepath.Join(path, SkillMarkdownFile))
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if string(got) != string(content) {
		t.Error("content was not stored verbatim")
	}
	for _, leftover := range []string{path + ".new", path + ".old"} {
		if _, err := os.Stat(leftover); !os.IsNotExist(err) {
			t.Errorf("%s was left behind", leftover)
		}
	}
}

func TestInstallSkillMarkdownRejectsBadFrontMatter(t *testing.T) {
	dir := t.TempDir()
	if _, err := InstallSkillMarkdown(dir, []byte("---\nname: Not A Slug\ndescription: d\n---\n")); !errors.Is(err, ErrInvalidSkillName) {
		t.Errorf("bad slug: err = %v, want ErrInvalidSkillName", err)
	}
	if _, err := InstallSkillMarkdown(dir, []byte("# no front-matter")); !errors.Is(err, ErrInvalidFrontMatter) {
		t.Errorf("no front-matter: err = %v, want ErrInvalidFrontMatter", err)
	}
	entries, _ := os.ReadDir(dir)
	if len(entries) != 0 {
		t.Errorf("nothing should have been written, found %d entries", len(entries))
	}
}

// Unlike authoring, installing replaces.
func TestInstallSkillMarkdownReplaces(t *testing.T) {
	dir := t.TempDir()
	seedSkill(t, dir, "x", map[string]string{"SKILL.md": "OLD", "stale.md": "stale"})

	if _, err := InstallSkillMarkdown(dir, []byte("---\nname: x\ndescription: d\n---\nNEW")); err != nil {
		t.Fatalf("install: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "x", "stale.md")); !os.IsNotExist(err) {
		t.Error("stale sibling survived — install is not a clean swap")
	}
}

// An archive with no SKILL.md at the skill root isn't a skill.
func TestInstallSkillArchiveRequiresSkillMD(t *testing.T) {
	tmp := t.TempDir()
	archive := filepath.Join(tmp, "a.zip")
	makeZip(t, archive, map[string]string{
		"my-skill/README.md":         "not the entry point",
		"my-skill/reference/note.md": "x",
	})
	skillsDir := filepath.Join(tmp, "skills")

	if _, _, err := InstallSkillArchive(archive, skillsDir, "my-skill"); !errors.Is(err, ErrMissingSkillMD) {
		t.Fatalf("err = %v, want ErrMissingSkillMD", err)
	}
	for _, p := range []string{
		filepath.Join(skillsDir, "my-skill"),
		filepath.Join(skillsDir, "my-skill.new"),
	} {
		if _, err := os.Stat(p); !os.IsNotExist(err) {
			t.Errorf("%s was left behind", p)
		}
	}
}

// SKILL.md must be at the skill ROOT, not buried in a subdirectory.
func TestInstallSkillArchiveRejectsNestedSkillMD(t *testing.T) {
	tmp := t.TempDir()
	archive := filepath.Join(tmp, "a.zip")
	makeZip(t, archive, map[string]string{"my-skill/docs/SKILL.md": "buried"})

	if _, _, err := InstallSkillArchive(archive, filepath.Join(tmp, "skills"), "my-skill"); !errors.Is(err, ErrMissingSkillMD) {
		t.Fatalf("err = %v, want ErrMissingSkillMD", err)
	}
}
