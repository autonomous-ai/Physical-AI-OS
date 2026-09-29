package skills

import (
	"archive/zip"
	"os"
	"path/filepath"
	"testing"
)

// writeZip builds a zip at path with the given name→content entries.
func writeZip(t *testing.T, path string, files map[string]string) {
	t.Helper()
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	w := zip.NewWriter(f)
	for name, content := range files {
		e, err := w.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := e.Write([]byte(content)); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
}

// ExtractSkillZip must replace the target, not merge into it.
func TestExtractSkillZip_AtomicReplace(t *testing.T) {
	dir := t.TempDir()
	target := filepath.Join(dir, "my-skill")

	z1 := filepath.Join(dir, "v1.zip")
	writeZip(t, z1, map[string]string{"SKILL.md": "v1", "old.txt": "gone-next"})
	if err := ExtractSkillZip(z1, target); err != nil {
		t.Fatalf("extract v1: %v", err)
	}
	if b, _ := os.ReadFile(filepath.Join(target, "SKILL.md")); string(b) != "v1" {
		t.Errorf("SKILL.md = %q, want v1", b)
	}

	z2 := filepath.Join(dir, "v2.zip")
	writeZip(t, z2, map[string]string{"SKILL.md": "v2"})
	if err := ExtractSkillZip(z2, target); err != nil {
		t.Fatalf("extract v2: %v", err)
	}
	if b, _ := os.ReadFile(filepath.Join(target, "SKILL.md")); string(b) != "v2" {
		t.Errorf("SKILL.md = %q, want v2", b)
	}
	if _, err := os.Stat(filepath.Join(target, "old.txt")); !os.IsNotExist(err) {
		t.Error("old.txt lingered — extract merged instead of replacing")
	}
}

// A zip entry escaping the target dir must be rejected (path-traversal guard).
func TestExtractSkillZip_RejectsTraversal(t *testing.T) {
	dir := t.TempDir()
	z := filepath.Join(dir, "evil.zip")
	writeZip(t, z, map[string]string{"../escape.txt": "pwned"})
	if err := ExtractSkillZip(z, filepath.Join(dir, "skill")); err == nil {
		t.Error("expected path-traversal rejection, got nil")
	}
}

func TestFolderHash_DetectsChange(t *testing.T) {
	dir := t.TempDir()
	a := filepath.Join(dir, "a")
	os.MkdirAll(a, 0o755)
	os.WriteFile(filepath.Join(a, "f"), []byte("x"), 0o644)
	h1, _ := FolderHash(a)
	h2, _ := FolderHash(a)
	if h1 == "" || h1 != h2 {
		t.Fatalf("hash not stable: %q vs %q", h1, h2)
	}
	os.WriteFile(filepath.Join(a, "f"), []byte("y"), 0o644)
	if h3, _ := FolderHash(a); h3 == h1 {
		t.Error("hash unchanged after content change")
	}
}

func TestFetchSkillVersions_EmptyURL(t *testing.T) {
	got, err := FetchSkillVersions("")
	if err != nil || got != nil {
		t.Errorf("empty URL: got (%v, %v), want (nil, nil)", got, err)
	}
}
