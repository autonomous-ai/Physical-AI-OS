package migratepersona

import (
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"
)

// quarantineRotateBytes caps the sidecar (rotated to `.1`) so a runaway agent cannot fill the disk.
const quarantineRotateBytes = 64 * 1024

// guardBackupsKept caps the guard's `.bak-<nano>` copies per file (heartbeat churn); the retire
// pass keeps its own backups.
const guardBackupsKept = 5

// GuardAction is one file the guard changed (execute=true) or would change.
type GuardAction struct {
	Path    string
	Dropped []Quarantined
	Written bool
	// WrittenSha8 is Sha8 of what the guard wrote (Written only), matched by the watcher.
	WrittenSha8 string
}

// MemoryFilePaths returns every runtime's MEMORY.md, deduped and sorted.
func MemoryFilePaths(opts Options) []string {
	seen := map[string]bool{}
	var out []string
	for _, a := range adapters {
		p := a.memoryFilePath(opts)
		if p == "" || seen[p] {
			continue
		}
		seen[p] = true
		out = append(out, p)
	}
	sort.Strings(out)
	return out
}

// QuarantinePath is the sidecar for path. Must be `.txt`, never `.md`: HAL globs memory/*.md
// into the realtime session and would re-inject the removed text.
func QuarantinePath(path string) string { return path + ".quarantine.txt" }

// GuardMemoryFile guards path (USER.md: allowlist, else MEMORY.md rule). Absent or clean files
// return (nil, nil) and are not written; on drop with execute: backup, sidecar, atomic replace.
func GuardMemoryFile(path string, enrolled map[string]bool, execute bool) (*GuardAction, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var out string
	var dropped []Quarantined
	if strings.EqualFold(filepath.Base(path), "USER.md") {
		out, dropped = GuardUserProfileText(string(raw), enrolled)
	} else {
		out, dropped = GuardMemoryText(string(raw))
	}
	if len(dropped) == 0 {
		return nil, nil
	}
	act := &GuardAction{Path: path, Dropped: dropped}
	if !execute {
		return act, nil
	}
	if err := backupFile(path); err != nil {
		return act, fmt.Errorf("backup before quarantine: %w", err)
	}
	if err := pruneBackups(path, guardBackupsKept); err != nil {
		// The fresh backup is on disk; a prune failure must not block cleaning.
		slog.Warn("memory guard: prune old backups failed", "component", "memory-guard", "path", path, "error", err)
	}
	if err := appendQuarantine(path, dropped); err != nil {
		return act, fmt.Errorf("write quarantine: %w", err)
	}
	if err := writeFileAtomic(path, out); err != nil {
		return act, err
	}
	act.Written = true
	act.WrittenSha8 = Sha8([]byte(out))
	return act, nil
}

// pruneBackups keeps the newest keep `<path>.bak-<n>` files; non-numeric suffixes are left alone.
func pruneBackups(path string, keep int) error {
	matches, err := filepath.Glob(path + ".bak-*")
	if err != nil {
		return fmt.Errorf("glob backups: %w", err)
	}
	type bak struct {
		name string
		n    int64
	}
	var baks []bak
	for _, m := range matches {
		n, err := strconv.ParseInt(strings.TrimPrefix(m, path+".bak-"), 10, 64)
		if err != nil {
			continue
		}
		baks = append(baks, bak{name: m, n: n})
	}
	if len(baks) <= keep {
		return nil
	}
	sort.Slice(baks, func(i, j int) bool { return baks[i].n > baks[j].n })
	for _, b := range baks[keep:] {
		if err := os.Remove(b.name); err != nil && !os.IsNotExist(err) {
			return fmt.Errorf("remove %s: %w", b.name, err)
		}
	}
	return nil
}

// GuardMemoryFiles sweeps every runtime's USER.md and MEMORY.md (an unguarded copy migrates
// back on switch). An unreadable enrollment store only skips the label check.
func GuardMemoryFiles(opts Options, execute bool) ([]GuardAction, error) {
	enrolled, err := enrolledLabels()
	if err != nil {
		slog.Warn("memory guard: enrollment store unreadable; label check skipped",
			"component", "memory-guard", "error", err)
		enrolled = nil
	}
	var actions []GuardAction
	for _, p := range append(UserProfilePaths(opts), MemoryFilePaths(opts)...) {
		act, err := GuardMemoryFile(p, enrolled, execute)
		if err != nil {
			return actions, fmt.Errorf("guard %s: %w", p, err)
		}
		if act != nil {
			actions = append(actions, *act)
		}
	}
	return actions, nil
}

// appendQuarantine appends dropped blocks to the sidecar, rotating past quarantineRotateBytes.
func appendQuarantine(path string, dropped []Quarantined) error {
	side := QuarantinePath(path)
	if st, err := os.Stat(side); err == nil && st.Size() > quarantineRotateBytes {
		if err := os.Rename(side, side+".1"); err != nil {
			return fmt.Errorf("rotate: %w", err)
		}
	}
	f, err := os.OpenFile(side, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	var sb strings.Builder
	fmt.Fprintf(&sb, "## %s\n", time.Now().Format(time.RFC3339))
	for _, d := range dropped {
		fmt.Fprintf(&sb, "- (%s) %s\n", d.Reason, d.Text)
	}
	sb.WriteString("\n")
	_, err = f.WriteString(sb.String())
	return err
}

// writeFileAtomic writes via temp file + rename; required because the agent may read mid-turn.
func writeFileAtomic(path, content string) error {
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return fmt.Errorf("create temp: %w", err)
	}
	tmpName := tmp.Name()
	defer func() { _ = os.Remove(tmpName) }()

	if _, err := tmp.WriteString(content); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write temp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp: %w", err)
	}
	if err := os.Chmod(tmpName, 0o644); err != nil {
		return fmt.Errorf("chmod temp: %w", err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("rename: %w", err)
	}
	return nil
}

// EnrolledLabels exposes enrolledLabels; nil when the store is unreadable (label check skipped).
func EnrolledLabels() map[string]bool {
	m, err := enrolledLabels()
	if err != nil {
		return nil
	}
	return m
}

// RuntimeOfPath names the runtime whose USER.md or MEMORY.md path is, or "".
func RuntimeOfPath(opts Options, path string) string {
	for r, a := range adapters {
		if a.userProfilePath(opts) == path || a.memoryFilePath(opts) == path {
			return string(r)
		}
	}
	return ""
}
