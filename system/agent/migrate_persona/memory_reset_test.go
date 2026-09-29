package migratepersona

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestResetMemoryFilesBacksUpAndClearsEveryRuntime(t *testing.T) {
	root := t.TempDir()
	ows := filepath.Join(root, "openclaw", "workspace")
	hroot := filepath.Join(root, "hermes")
	for _, d := range []string{ows, filepath.Join(ows, "realtime"), filepath.Join(hroot, "memories"), filepath.Join(hroot, "realtime")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	files := map[string]string{
		filepath.Join(ows, "USER.md"):                    "- poison\n",
		filepath.Join(ows, "MEMORY.md"):                  "- poison\n",
		filepath.Join(ows, "KNOWLEDGE.md"):               "# k\n",
		filepath.Join(ows, "realtime", "summary.md"):     "pending: turn off tv\n",
		filepath.Join(hroot, "memories", "USER.md"):      "poison\n§\nmore\n",
		filepath.Join(hroot, "memories", "MEMORY.md"):    "poison\n",
		filepath.Join(hroot, "realtime", "memory.jsonl"): "{}\n",
	}
	for p, body := range files {
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	opts := Options{OpenclawWorkspace: ows, HermesRoot: hroot}.withDefaults()

	rep, err := ResetMemoryFiles(opts)
	if err != nil {
		t.Fatalf("reset: %v", err)
	}
	if len(rep.Cleared) != len(files) {
		t.Fatalf("want %d cleared, got %d: %v", len(files), len(rep.Cleared), rep.Cleared)
	}
	if len(rep.BackupDirs) != 2 {
		t.Fatalf("want one backup dir per touched runtime, got %v", rep.BackupDirs)
	}
	if got := readFile(t, filepath.Join(ows, "USER.md")); !strings.Contains(got, "- **Name:**") || strings.Contains(got, "poison") {
		t.Errorf("openclaw USER.md not reset to the form:\n%s", got)
	}
	if got := readFile(t, filepath.Join(hroot, "memories", "USER.md")); got != "" {
		t.Errorf("hermes USER.md must be emptied, got %q", got)
	}
	for _, p := range []string{filepath.Join(ows, "MEMORY.md"), filepath.Join(ows, "KNOWLEDGE.md"), filepath.Join(ows, "realtime", "summary.md"), filepath.Join(hroot, "realtime", "memory.jsonl")} {
		if _, err := os.Stat(p); !os.IsNotExist(err) {
			t.Errorf("%s must be removed", p)
		}
	}
	bak, _ := filepath.Glob(filepath.Join(ows, ".memory-reset-*", "USER.md"))
	if len(bak) != 1 || readFile(t, bak[0]) != "- poison\n" {
		t.Errorf("backup of openclaw USER.md missing or wrong: %v", bak)
	}
}

func TestResetMemoryFilesSkipsUninstalledRuntimes(t *testing.T) {
	opts := Options{OpenclawWorkspace: filepath.Join(t.TempDir(), "nope")}.withDefaults()
	rep, err := ResetMemoryFiles(opts)
	if err != nil || len(rep.Cleared) != 0 || len(rep.BackupDirs) != 0 {
		t.Fatalf("absent workspaces must be a no-op, got %+v err=%v", rep, err)
	}
}

// TestResetMemoryFilesTwiceKeepsTheFirstBackup: same-second calls get distinct backup dirs.
func TestResetMemoryFilesTwiceKeepsTheFirstBackup(t *testing.T) {
	ows := filepath.Join(t.TempDir(), "workspace")
	if err := os.MkdirAll(ows, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(ows, "USER.md"), []byte("- poison\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	opts := Options{OpenclawWorkspace: ows}.withDefaults()

	first, err := ResetMemoryFiles(opts)
	if err != nil || len(first.BackupDirs) != 1 {
		t.Fatalf("first reset: %+v err=%v", first, err)
	}
	second, err := ResetMemoryFiles(opts)
	if err != nil || len(second.BackupDirs) != 1 {
		t.Fatalf("second reset: %+v err=%v", second, err)
	}
	if first.BackupDirs[0] == second.BackupDirs[0] {
		t.Fatalf("both resets used the same backup dir %s", first.BackupDirs[0])
	}
	dirs, _ := filepath.Glob(filepath.Join(ows, ".memory-reset-*"))
	if len(dirs) != 2 {
		t.Fatalf("want 2 backup dirs, got %v", dirs)
	}
	if got := readFile(t, filepath.Join(first.BackupDirs[0], "USER.md")); got != "- poison\n" {
		t.Errorf("first backup overwritten, got %q", got)
	}
	if got := readFile(t, filepath.Join(second.BackupDirs[0], "USER.md")); got != userProfileResetForm {
		t.Errorf("second backup should hold the blank form, got %q", got)
	}
}
