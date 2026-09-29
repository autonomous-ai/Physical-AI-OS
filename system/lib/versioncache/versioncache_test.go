package versioncache

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

// writeBin writes size bytes to path and stamps a distinct mtime so two writes
// never produce the same stamp on a coarse-grained filesystem clock.
func writeBin(t *testing.T, path string, size int, mtime time.Time) {
	t.Helper()
	if err := os.WriteFile(path, make([]byte, size), 0755); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
	if err := os.Chtimes(path, mtime, mtime); err != nil {
		t.Fatalf("chtimes %s: %v", path, err)
	}
}

// waitFor polls cond for up to a second — the refresh probe runs in its own
// goroutine, so the new version lands shortly after the Get that triggered it.
func waitFor(t *testing.T, cond func() bool) bool {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return true
		}
		time.Sleep(5 * time.Millisecond)
	}
	return false
}

func TestGetRefreshesAfterBinaryChange(t *testing.T) {
	bin := filepath.Join(t.TempDir(), "codex")
	writeBin(t, bin, 10, time.Now().Add(-time.Hour))

	var probes atomic.Int64
	c := New(bin, "test-probe", func(context.Context) (string, bool) {
		return fmt.Sprintf("1.0.%d", probes.Add(1)), true
	})

	c.Populate(0, 0)
	if got := c.Get(); got != "1.0.1" {
		t.Fatalf("after Populate: got %q, want 1.0.1", got)
	}
	// Unchanged binary must not cost a probe.
	_ = c.Get()
	if n := probes.Load(); n != 1 {
		t.Fatalf("unchanged binary triggered %d probes, want 1", n)
	}

	// An update lands as a new build of the binary.
	writeBin(t, bin, 20, time.Now())
	_ = c.Get() // kicks the async re-probe
	if !waitFor(t, func() bool { return c.Get() == "1.0.2" }) {
		t.Fatalf("version not refreshed after binary change: got %q", c.Get())
	}
}

func TestFailedProbeDoesNotReprobeUntilBinaryChanges(t *testing.T) {
	bin := filepath.Join(t.TempDir(), "hermes")
	writeBin(t, bin, 10, time.Now().Add(-time.Hour))

	var probes atomic.Int64
	c := New(bin, "test-probe", func(context.Context) (string, bool) {
		probes.Add(1)
		return "", false
	})

	c.Populate(0, 0)
	if got := c.Get(); got != "" {
		t.Fatalf("failed probe stored %q, want empty", got)
	}
	for i := 0; i < 5; i++ {
		_ = c.Get()
	}
	if !waitFor(t, func() bool { return probes.Load() > 1 }) {
		return // expected: the stamp claimed by Populate suppresses the retries
	}
	t.Fatalf("failing probe ran %d times without a binary change, want 1", probes.Load())
}

func TestMissingBinaryKeepsCachedVersion(t *testing.T) {
	bin := filepath.Join(t.TempDir(), "opencode")
	writeBin(t, bin, 10, time.Now().Add(-time.Hour))

	c := New(bin, "test-probe", func(context.Context) (string, bool) { return "2.3.4", true })
	c.Populate(0, 0)
	if err := os.Remove(bin); err != nil {
		t.Fatalf("remove %s: %v", bin, err)
	}
	if got := c.Get(); got != "2.3.4" {
		t.Fatalf("got %q after binary removal, want the cached 2.3.4", got)
	}
}
