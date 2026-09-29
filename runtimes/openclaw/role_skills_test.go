package openclaw

import (
	"archive/zip"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestInstallRoleSkillsInvalidRole(t *testing.T) {
	for _, role := range []string{"", "../etc", "Bad", "a/b", "x.y", "UP", "a b"} {
		if _, err := InstallRoleSkills(t.TempDir(), role); !errors.Is(err, ErrInvalidRole) {
			t.Errorf("role %q: want ErrInvalidRole, got %v", role, err)
		}
	}
}

func TestExtractDirFromZip(t *testing.T) {
	dir := t.TempDir()
	zipPath := filepath.Join(dir, "skills.zip")

	f, err := os.Create(zipPath)
	if err != nil {
		t.Fatal(err)
	}
	w := zip.NewWriter(f)
	write := func(name, body string) {
		wr, err := w.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := wr.Write([]byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	write("skills/foo/SKILL.md", "hello")
	write("skills/bar.txt", "x")
	write("other/ignore.txt", "should be skipped")
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	f.Close()

	dest := filepath.Join(dir, "out")
	n, err := extractDirFromZip(zipPath, "skills/", dest)
	if err != nil {
		t.Fatalf("extractDirFromZip: %v", err)
	}
	if n != 2 {
		t.Errorf("file count = %d, want 2", n)
	}
	if b, err := os.ReadFile(filepath.Join(dest, "foo", "SKILL.md")); err != nil || string(b) != "hello" {
		t.Errorf("foo/SKILL.md: body=%q err=%v", b, err)
	}
	if _, err := os.Stat(filepath.Join(dest, "bar.txt")); err != nil {
		t.Errorf("bar.txt missing: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dest, "ignore.txt")); !os.IsNotExist(err) {
		t.Errorf("ignore.txt should not be extracted")
	}
}
