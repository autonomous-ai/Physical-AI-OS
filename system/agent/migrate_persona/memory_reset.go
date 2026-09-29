package migratepersona

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

// resetMu serialises resets so two calls never share or overwrite a backup dir.
var resetMu sync.Mutex

// ResetReport lists what ResetMemoryFiles backed up and cleared.
type ResetReport struct {
	BackupDirs []string `json:"backup_dirs"`
	Cleared    []string `json:"cleared"`
	Skipped    []string `json:"skipped"` // targets that did not exist
}

// userProfileResetForm is the blank USER.md form markdown runtimes get back.
// The `## Users` heading is included so the agent's people sync has its slot.
const userProfileResetForm = `- _Learn about the person you're helping. Update this as you go._
- **Name:**
- **What to call them:**
- **Pronouns:** _(optional)_
- **Timezone:**

## Users
`

// realtimeMemoryFiles are HAL's realtime-layer memory under <workspace>/realtime/
// (see hal/config.py REALTIME_MEMORY_PATH and context_manager/base.py).
var realtimeMemoryFiles = []string{"summary.md", "device_summary.md", "memory.jsonl", "memory_raw.jsonl"}

// ResetMemoryFiles backs up and clears USER.md, MEMORY.md, KNOWLEDGE.md and realtime memory for
// every installed runtime (#421); session history is untouched. On error the partial report is returned.
func ResetMemoryFiles(opts Options) (ResetReport, error) {
	resetMu.Lock()
	defer resetMu.Unlock()

	var rep ResetReport
	stamp := time.Now().Format("20060102-150405")

	runtimes := make([]string, 0, len(adapters))
	for r := range adapters {
		runtimes = append(runtimes, string(r))
	}
	sort.Strings(runtimes)

	for _, r := range runtimes {
		a := adapters[Runtime(r)]
		root := a.workspaceRoot(opts)
		if root == "" {
			continue
		}
		if st, err := os.Stat(root); err != nil || !st.IsDir() {
			continue
		}
		user := a.userProfilePath(opts)
		targets := []string{user, a.memoryFilePath(opts), filepath.Join(filepath.Dir(user), "KNOWLEDGE.md")}
		for _, f := range realtimeMemoryFiles {
			targets = append(targets, filepath.Join(root, "realtime", f))
		}
		// Backup dir is created lazily and unique per call; recorded immediately for the error path.
		bak := ""
		for _, p := range targets {
			data, err := os.ReadFile(p)
			if err != nil {
				if os.IsNotExist(err) {
					rep.Skipped = append(rep.Skipped, p)
					continue
				}
				return rep, fmt.Errorf("read %s: %w", p, err)
			}
			if bak == "" {
				bak, err = os.MkdirTemp(root, ".memory-reset-"+stamp+"-*")
				if err != nil {
					return rep, fmt.Errorf("create backup dir: %w", err)
				}
				rep.BackupDirs = append(rep.BackupDirs, bak)
			}
			if err := writeBackup(filepath.Join(bak, filepath.Base(p)), data); err != nil {
				return rep, fmt.Errorf("backup %s: %w", p, err)
			}
			switch {
			case p == user && Runtime(r) == RuntimeHermes:
				err = writeFileAtomic(p, "")
			case p == user:
				err = writeFileAtomic(p, userProfileResetForm)
			default:
				err = os.Remove(p)
			}
			if err != nil {
				return rep, fmt.Errorf("clear %s: %w", p, err)
			}
			rep.Cleared = append(rep.Cleared, p)
		}
	}
	return rep, nil
}

// writeBackup writes and fsyncs a backup before the original is removed (SD card);
// O_EXCL because the dir is fresh per call.
func writeBackup(dst string, data []byte) error {
	f, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return fmt.Errorf("create: %w", err)
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		return fmt.Errorf("write: %w", err)
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return fmt.Errorf("fsync: %w", err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("close: %w", err)
	}
	return nil
}
