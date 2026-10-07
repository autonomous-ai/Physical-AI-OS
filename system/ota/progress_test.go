package ota

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

func writeProgress(t *testing.T, dir, target string, p Progress) {
	t.Helper()
	raw, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, target+".json"), raw, 0600); err != nil {
		t.Fatal(err)
	}
}

func TestReadProgressRecoversInterruptedAndPreservesLiveWork(t *testing.T) {
	now := time.Unix(1700000000, 0)
	for _, tc := range []struct {
		name, phase, want string
		age               int64
		alive             bool
	}{
		{"recent dead process", "installing", "installing", 10, false},
		{"dead installer", "installing", "interrupted", 31, false},
		{"long running installer", "installing", "installing", 300, true},
		{"completed survives restart", "completed", "completed", 300, false},
		{"failed survives restart", "failed", "failed", 300, false},
		{"rollback interrupted", "rolling_back", "interrupted", 31, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			writeProgress(t, dir, "hermes", Progress{Target: "hermes", RunID: "run-1", Phase: tc.phase, UpdatedAt: now.Unix() - tc.age, PID: 123})
			got := readProgress(dir, hermesCfg(), now, func(int) bool { return tc.alive }, "")
			if len(got) != 2 || got["hermes"].Phase != tc.want || got["agent"] != got["hermes"] {
				t.Fatalf("progress = %#v", got)
			}
		})
	}
}

func TestReadProgressIgnoresInvalidFiles(t *testing.T) {
	dir := t.TempDir()
	valid := Progress{Target: "hal", RunID: "run-2", Phase: "downloading", UpdatedAt: 1700000000, PID: 123, DownloadedBytes: 50, TotalBytes: 100}
	writeProgress(t, dir, "hal", valid)
	// An unknown filename is never read, even if it contains valid JSON.
	writeProgress(t, dir, "unknown", valid)
	writeProgress(t, dir, "web", valid) // target mismatch
	invalid := valid
	invalid.Target = "hermes"
	invalid.Phase = "fictional"
	writeProgress(t, dir, "hermes", invalid)
	invalid = valid
	invalid.Target = "codex"
	invalid.DownloadedBytes = -1
	writeProgress(t, dir, "codex", invalid)
	for name, raw := range map[string]string{"bootstrap": "{", "os-server": strings.Repeat(" ", maxProgressSize+1)} {
		if err := os.WriteFile(filepath.Join(dir, name+".json"), []byte(raw), 0600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink(filepath.Join(dir, "hal.json"), filepath.Join(dir, "opencode.json")); err != nil {
		t.Fatal(err)
	}
	got := readProgress(dir, hermesCfg(), time.Unix(1700000000, 0), func(int) bool { return true }, "")
	if len(got) != 1 || got["hal"] != valid {
		t.Fatalf("progress = %#v", got)
	}
}

func TestCombineProgressWorkerFallback(t *testing.T) {
	progress := map[string]Progress{
		"hermes": {Phase: "installing"}, "agent": {Phase: "installing"},
		"hal": {Phase: "completed"}, "web": {Phase: "interrupted"},
	}
	fallback := combineProgress(nil, false, progress)
	if fallback.BootstrapAvailable || !reflect.DeepEqual(fallback.Updating, []string{"agent", "hermes"}) || len(fallback.Progress) != 4 {
		t.Fatalf("fallback = %#v", fallback)
	}
	live := combineProgress([]string{"hermes", "bootstrap"}, true, progress)
	if !live.BootstrapAvailable || !reflect.DeepEqual(live.Updating, []string{"agent", "bootstrap", "hermes"}) {
		t.Fatalf("live = %#v", live)
	}
	empty := combineProgress(nil, false, map[string]Progress{})
	raw, err := json.Marshal(empty)
	if err != nil || string(raw) != `{"updating":[],"progress":{},"bootstrap_available":false}` {
		t.Fatalf("empty JSON = %s, %v", raw, err)
	}
}

func TestReadProgressDetectsRebootWithReusedPID(t *testing.T) {
	dir := t.TempDir()
	writeProgress(t, dir, "hal", Progress{Target: "hal", RunID: "run", Phase: "checking", UpdatedAt: 1700000000, PID: 123, BootID: "old-boot"})
	got := readProgress(dir, hermesCfg(), time.Unix(1700000100, 0), func(int) bool { return true }, "new-boot")
	if got["hal"].Phase != "interrupted" {
		t.Fatalf("progress = %#v", got)
	}
}
