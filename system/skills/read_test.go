package skills

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestReadSkillFiles(t *testing.T) {
	dir := t.TempDir()
	seedSkill(t, dir, "music", map[string]string{
		"SKILL.md":           "---\ndescription: Play music.\n---\n\nbody",
		"reference/tempo.md": "tempo notes",
		"assets/icon.png":    "\x89PNG\x00\x01binary",
		".hidden":            "skip me",
	})

	files, err := ReadSkillFiles(dir, "music")
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if len(files) != 3 {
		paths := []string{}
		for _, f := range files {
			paths = append(paths, f.Path)
		}
		t.Fatalf("want 3 files (dotfile skipped), got %d: %v", len(files), paths)
	}

	byPath := map[string]int{}
	for i, f := range files {
		byPath[f.Path] = i
	}
	if _, ok := byPath["music/reference/tempo.md"]; !ok {
		t.Fatalf("nested file missing: %v", byPath)
	}
	if files[0].Path != "music/SKILL.md" {
		t.Errorf("not sorted by path, first = %q", files[0].Path)
	}

	skillMD := files[byPath["music/SKILL.md"]]
	if !strings.Contains(skillMD.Text, "Play music.") || skillMD.Binary {
		t.Errorf("SKILL.md: text=%q binary=%v", skillMD.Text, skillMD.Binary)
	}
	if skillMD.Size == 0 {
		t.Error("size not populated")
	}

	icon := files[byPath["music/assets/icon.png"]]
	if !icon.Binary || icon.Text != "" {
		t.Errorf("icon.png must be binary with no text: binary=%v text=%q", icon.Binary, icon.Text)
	}
}

func TestReadSkillFilesRejectsBadName(t *testing.T) {
	dir := t.TempDir()
	seedSkill(t, dir, "music", map[string]string{"SKILL.md": "x"})
	if err := os.WriteFile(filepath.Join(dir, "secret.md"), []byte("s"), 0644); err != nil {
		t.Fatal(err)
	}

	for _, name := range []string{"", "../", "../secret", "a/b", "Music"} {
		if _, err := ReadSkillFiles(dir, name); err == nil {
			t.Errorf("name %q must be rejected", name)
		} else if name != "" && !errors.Is(err, ErrInvalidSkillName) {
			t.Errorf("name %q: err = %v, want ErrInvalidSkillName", name, err)
		}
	}
}

func TestReadSkillFilesMissing(t *testing.T) {
	if _, err := ReadSkillFiles(t.TempDir(), "nope"); err == nil {
		t.Fatal("a missing skill must be an error (the handler maps it to 404)")
	}
}

func TestReadSkillFile(t *testing.T) {
	dir := t.TempDir()
	seedSkill(t, dir, "music", map[string]string{
		"SKILL.md":           "skill instructions",
		"reference/tempo.md": "tempo notes",
	})

	file, err := ReadSkillFile(dir, "music", "music/reference/tempo.md")
	if err != nil {
		t.Fatalf("read file: %v", err)
	}
	if file.Path != "music/reference/tempo.md" || file.Text != "tempo notes" {
		t.Errorf("file = %+v", file)
	}

	// MQTT paths must be exact list entries (no basenames, cleaned variants or dotfiles).
	for _, bad := range []string{"tempo.md", "music/../music/SKILL.md", "music/.secret", "music/missing.md"} {
		if _, err := ReadSkillFile(dir, "music", bad); !errors.Is(err, ErrSkillFileNotFound) {
			t.Errorf("path %q: err = %v, want ErrSkillFileNotFound", bad, err)
		}
	}
}

// Roots are tried in order; the first match wins.
func TestReadSkillFilesFrom(t *testing.T) {
	base := t.TempDir()
	authored := filepath.Join(base, "authored")
	imported := filepath.Join(base, "openclaw-imports")
	seedSkill(t, authored, "music", map[string]string{"SKILL.md": "MINE"})
	seedSkill(t, imported, "music", map[string]string{"SKILL.md": "IMPORTED"})
	seedSkill(t, imported, "voice", map[string]string{"SKILL.md": "VOICE"})

	files, err := ReadSkillFilesFrom("music", authored, imported)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if len(files) != 1 || files[0].Text != "MINE" {
		t.Errorf("first root must win, got %+v", files)
	}

	files, err = ReadSkillFilesFrom("voice", authored, imported)
	if err != nil {
		t.Fatalf("read voice: %v", err)
	}
	if len(files) != 1 || files[0].Text != "VOICE" {
		t.Errorf("second root not searched, got %+v", files)
	}

	if _, err := ReadSkillFilesFrom("absent", authored, imported); err == nil {
		t.Error("a skill in neither root must error")
	}
}

func TestBuildFilePreviewTruncates(t *testing.T) {
	big := strings.Repeat("a", readMaxInlineBytes+100)
	f := BuildFilePreview("x/big.md", []byte(big), int64(len(big)))

	if !f.Truncated {
		t.Error("oversized text must be marked truncated")
	}
	if len(f.Text) > readMaxInlineBytes {
		t.Errorf("inlined %d bytes, cap is %d", len(f.Text), readMaxInlineBytes)
	}
	if f.Size != int64(len(big)) {
		t.Errorf("size = %d, want %d", f.Size, len(big))
	}
}
