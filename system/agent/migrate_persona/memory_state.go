package migratepersona

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"sync/atomic"
)

// memoryState is the active runtime's memory fingerprint (sizes and hashes, never content),
// attached to each turn's lifecycle_start flow event.
var memoryState atomic.Pointer[map[string]any]

// SetMemoryState replaces the published fingerprint.
func SetMemoryState(m map[string]any) { memoryState.Store(&m) }

// MemoryState returns the published fingerprint, or nil before the first sweep.
func MemoryState() map[string]any {
	p := memoryState.Load()
	if p == nil {
		return nil
	}
	return *p
}

// ReadMemoryState fingerprints the files runtime loads: USER.md, MEMORY.md and,
// when present beside USER.md, KNOWLEDGE.md. Absent files are omitted.
func ReadMemoryState(runtime Runtime, opts Options) map[string]any {
	a, ok := adapters[runtime]
	if !ok {
		return nil
	}
	user := a.userProfilePath(opts)
	paths := map[string]string{
		"USER.md":      user,
		"MEMORY.md":    a.memoryFilePath(opts),
		"KNOWLEDGE.md": filepath.Join(filepath.Dir(user), "KNOWLEDGE.md"),
	}
	out := map[string]any{}
	for name, p := range paths {
		if p == "" {
			continue
		}
		raw, err := os.ReadFile(p)
		if err != nil {
			continue
		}
		out[name] = map[string]any{"size": len(raw), "sha8": Sha8(raw)}
	}
	return out
}

// Sha8 is the first 8 hex chars of sha256 — enough to see "same file or not".
func Sha8(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])[:8]
}
