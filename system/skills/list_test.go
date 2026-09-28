package skills

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func seedSkill(t *testing.T, skillsDir, name string, files map[string]string) {
	t.Helper()
	for rel, content := range files {
		p := filepath.Join(skillsDir, name, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(content), 0644); err != nil {
			t.Fatal(err)
		}
	}
}

func TestListInstalled(t *testing.T) {
	dir := t.TempDir()
	seedSkill(t, dir, "music", map[string]string{
		"SKILL.md":            "---\nname: music\ndescription: Play music.\n---\n\nbody",
		"reference/tempo.md":  "x",
		"reference/genres.md": "x",
		"scripts/resolve.py":  "x",
	})
	seedSkill(t, dir, "voice", map[string]string{"SKILL.md": "no front-matter here"})
	seedSkill(t, dir, "music.new", map[string]string{"SKILL.md": "x"})
	seedSkill(t, dir, "music.old", map[string]string{"SKILL.md": "x"})
	seedSkill(t, dir, ".hidden", map[string]string{"SKILL.md": "x"})

	list, err := ListInstalled(dir)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(list) != 2 {
		names := []string{}
		for _, s := range list {
			names = append(names, s.Name)
		}
		t.Fatalf("want 2 skills, got %d: %v", len(list), names)
	}
	if list[0].Name != "music" || list[1].Name != "voice" {
		t.Fatalf("unsorted: %s, %s", list[0].Name, list[1].Name)
	}
	if list[0].Description != "Play music." {
		t.Errorf("description = %q", list[0].Description)
	}
	if list[1].Description != "" {
		t.Errorf("voice description = %q, want empty", list[1].Description)
	}

	files := list[0].Files
	if len(files) != 3 {
		t.Fatalf("music files = %d, want 3", len(files))
	}
	if !files[0].Dir || files[0].Name != "reference" {
		t.Errorf("files[0] = %+v, want dir 'reference' first", files[0])
	}
	if files[2].Name != "SKILL.md" || files[2].Dir {
		t.Errorf("files[2] = %+v, want file SKILL.md last", files[2])
	}
	if files[0].Path != "music/reference" {
		t.Errorf("dir path = %q", files[0].Path)
	}
	if len(files[0].Children) != 2 || files[0].Children[0].Path != "music/reference/genres.md" {
		t.Errorf("children = %+v", files[0].Children)
	}
	if files[2].Size == 0 {
		t.Error("file size not populated")
	}
}

// UpdatedAt is the newest mtime anywhere in the tree, not the skill dir's own.
func TestListInstalledUpdatedAt(t *testing.T) {
	dir := t.TempDir()
	seedSkill(t, dir, "music", map[string]string{
		"SKILL.md":           "---\nname: music\ndescription: Play music.\n---\n",
		"reference/tempo.md": "x",
	})

	old := time.Now().Add(-72 * time.Hour)
	recent := time.Now().Add(-30 * time.Minute)
	skillDir := filepath.Join(dir, "music")
	for _, rel := range []string{"SKILL.md", "reference"} {
		if err := os.Chtimes(filepath.Join(skillDir, rel), old, old); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Chtimes(filepath.Join(skillDir, "reference", "tempo.md"), recent, recent); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(skillDir, old, old); err != nil {
		t.Fatal(err)
	}

	list, err := ListInstalled(dir)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(list) != 1 {
		t.Fatalf("want 1 skill, got %d", len(list))
	}
	if got := list[0].UpdatedAt; got != recent.Unix() {
		t.Errorf("UpdatedAt = %d, want %d (newest file in the tree)", got, recent.Unix())
	}
}

// A skill dir with nothing in it still reports when it appeared, rather than 0.
func TestListInstalledUpdatedAtEmptySkill(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "blank"), 0755); err != nil {
		t.Fatal(err)
	}

	list, err := ListInstalled(dir)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(list) != 1 || list[0].UpdatedAt == 0 {
		t.Fatalf("want the dir's own mtime as a fallback, got %+v", list)
	}
}

// An un-provisioned runtime has no skills dir yet — that is empty, not broken.
func TestListInstalledMissingDir(t *testing.T) {
	list, err := ListInstalled(filepath.Join(t.TempDir(), "nope"))
	if err != nil {
		t.Fatalf("missing dir must not error: %v", err)
	}
	if len(list) != 0 {
		t.Errorf("want empty list, got %d", len(list))
	}
}

func TestReadSkillDescription(t *testing.T) {
	dir := t.TempDir()
	cases := []struct{ label, content, want string }{
		{"plain", "---\nname: a\ndescription: Hello there.\n---\nbody", "Hello there."},
		{"quoted", "---\ndescription: \"Quoted value\"\n---\n", "Quoted value"},
		{"no front-matter", "# Just a heading\ndescription: not front-matter\n", ""},
		{"front-matter without description", "---\nname: a\n---\nbody", ""},
		{"empty file", "", ""},
	}
	for _, tc := range cases {
		t.Run(tc.label, func(t *testing.T) {
			p := filepath.Join(dir, tc.label+".md")
			if err := os.WriteFile(p, []byte(tc.content), 0644); err != nil {
				t.Fatal(err)
			}
			if got := readSkillDescription(p); got != tc.want {
				t.Errorf("got %q, want %q", got, tc.want)
			}
		})
	}
	if got := readSkillDescription(filepath.Join(dir, "absent.md")); got != "" {
		t.Errorf("absent file = %q, want empty", got)
	}
}

// Listing merges roots; the first root wins on a name clash.
func TestListInstalledFromMergesRoots(t *testing.T) {
	base := t.TempDir()
	authored := filepath.Join(base, "authored")
	imported := filepath.Join(base, "openclaw-imports")

	seedSkill(t, authored, "weekly-report", map[string]string{
		"SKILL.md": "---\ndescription: Authored.\n---\n",
	})
	seedSkill(t, authored, "music", map[string]string{
		"SKILL.md": "---\ndescription: Mine.\n---\n",
	})
	seedSkill(t, imported, "music", map[string]string{
		"SKILL.md": "---\ndescription: Imported.\n---\n",
	})
	seedSkill(t, imported, "voice", map[string]string{
		"SKILL.md": "---\ndescription: Imported voice.\n---\n",
	})

	list, err := ListInstalledFrom(authored, imported)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(list) != 3 {
		names := []string{}
		for _, s := range list {
			names = append(names, s.Name)
		}
		t.Fatalf("want 3 merged skills, got %d: %v", len(list), names)
	}
	byName := map[string]string{}
	for _, s := range list {
		byName[s.Name] = s.Description
	}
	if byName["music"] != "Mine." {
		t.Errorf("clash resolved to %q, want the first root's %q", byName["music"], "Mine.")
	}
	if byName["voice"] != "Imported voice." {
		t.Errorf("second root not merged in: %q", byName["voice"])
	}
	if list[0].Name != "music" || list[1].Name != "voice" || list[2].Name != "weekly-report" {
		t.Errorf("merged list not sorted: %s %s %s", list[0].Name, list[1].Name, list[2].Name)
	}
}

// A runtime whose roots don't exist yet must return an empty JSON array, not nil.
func TestListInstalledFromAllMissing(t *testing.T) {
	base := t.TempDir()
	list, err := ListInstalledFrom(filepath.Join(base, "a"), filepath.Join(base, "b"))
	if err != nil {
		t.Fatalf("missing roots must not error: %v", err)
	}
	if list == nil || len(list) != 0 {
		t.Errorf("want empty non-nil list, got %#v", list)
	}
}
