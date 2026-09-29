package versioncache

import (
	"context"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

func configureTest(t *testing.T, ready func(context.Context) bool, maxWait time.Duration) context.CancelFunc {
	t.Helper()
	previous := startup.Load()
	ctx, cancel := context.WithCancel(context.Background())
	ConfigureStartup(ctx, ready, maxWait)
	t.Cleanup(func() { cancel(); startup.Store(previous) })
	return cancel
}

func await(t *testing.T, ch <-chan struct{}) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for worker")
	}
}

func testCache(t *testing.T, probe func(context.Context) (string, bool)) *Cache {
	t.Helper()
	bin := filepath.Join(t.TempDir(), "cli")
	writeBin(t, bin, 10, time.Now())
	return New(bin, "test", probe)
}

func TestStartupGatesGetAndPopulateAndSerializes(t *testing.T) {
	checking, ready := make(chan struct{}), make(chan struct{})
	var checks atomic.Int32
	configureTest(t, func(ctx context.Context) bool {
		checks.Add(1)
		close(checking)
		select {
		case <-ready:
			return true
		case <-ctx.Done():
			return false
		}
	}, time.Minute)
	entered := make(chan struct{}, 2)
	release := make(chan struct{})
	var active, peak atomic.Int32
	probe := func(ctx context.Context) (string, bool) {
		n := active.Add(1)
		for old := peak.Load(); old < n && !peak.CompareAndSwap(old, n); old = peak.Load() {
		}
		entered <- struct{}{}
		select {
		case <-release:
		case <-ctx.Done():
		}
		active.Add(-1)
		return "1", true
	}
	first, second := testCache(t, probe), testCache(t, probe)
	done := make(chan struct{})
	go func() { first.Populate(0, 0); close(done) }()
	await(t, checking)
	if got := second.Get(); got != "" {
		t.Fatalf("unexpected cached version %q", got)
	}
	if !first.probing.Load() || !second.probing.Load() {
		t.Fatal("both paths must await readiness")
	}
	select {
	case <-entered:
		t.Fatal("probe ran before readiness")
	default:
	}
	close(ready)
	await(t, entered)
	select {
	case <-entered:
		t.Fatal("concurrent CLI probes")
	default:
	}
	close(release)
	await(t, done)
	if !waitFor(t, func() bool { return second.Get() == "1" && !second.probing.Load() }) {
		t.Fatal("Get did not complete")
	}
	if peak.Load() != 1 {
		t.Fatalf("maximum concurrent probes = %d", peak.Load())
	}
	third := testCache(t, func(context.Context) (string, bool) { return "2", true })
	third.Populate(0, 0)
	if checks.Load() != 1 {
		t.Fatal("readiness gate rechecked after opening")
	}
}

func TestStartupFallback(t *testing.T) {
	configureTest(t, func(ctx context.Context) bool { <-ctx.Done(); return false }, 10*time.Millisecond)
	c := testCache(t, func(context.Context) (string, bool) { return "1", true })
	done := make(chan struct{})
	go func() { c.Populate(0, 0); close(done) }()
	await(t, done)
	if c.Get() != "1" {
		t.Fatal("fallback did not admit probe")
	}
}

func TestStartupCancellationWhileWaiting(t *testing.T) {
	checking := make(chan struct{})
	cancel := configureTest(t, func(ctx context.Context) bool { close(checking); <-ctx.Done(); return false }, time.Minute)
	var calls atomic.Int32
	c := testCache(t, func(context.Context) (string, bool) { calls.Add(1); return "1", true })
	done := make(chan struct{})
	go func() { c.Populate(6, time.Hour); close(done) }()
	await(t, checking)
	cancel()
	await(t, done)
	if calls.Load() != 0 {
		t.Fatal("probe ran after cancellation")
	}
}

func TestStartupCancellationReachesProbe(t *testing.T) {
	cancel := configureTest(t, nil, 0)
	entered := make(chan struct{})
	var calls atomic.Int32
	c := testCache(t, func(ctx context.Context) (string, bool) { calls.Add(1); close(entered); <-ctx.Done(); return "", false })
	done := make(chan struct{})
	go func() { c.Populate(6, time.Hour); close(done) }()
	await(t, entered)
	cancel()
	await(t, done)
	if calls.Load() != 1 {
		t.Fatal("probe retried after cancellation")
	}
}

func TestRetryBackoffReleasesAdmissionAndCancels(t *testing.T) {
	cancel := configureTest(t, nil, 0)
	failed := make(chan struct{})
	var calls atomic.Int32
	c := testCache(t, func(context.Context) (string, bool) { calls.Add(1); close(failed); return "", false })
	done := make(chan struct{})
	go func() { c.Populate(6, time.Hour); close(done) }()
	await(t, failed)
	other := testCache(t, func(context.Context) (string, bool) { return "2", true })
	otherDone := make(chan struct{})
	go func() { other.Populate(0, 0); close(otherDone) }()
	await(t, otherDone)
	if other.Get() != "2" {
		t.Fatal("retry retained admission")
	}
	cancel()
	await(t, done)
	if calls.Load() != 1 {
		t.Fatal("canceled retry executed")
	}
}

func TestPopulateSkipsMissingBinary(t *testing.T) {
	configureTest(t, func(context.Context) bool { t.Error("missing CLI started readiness check"); return false }, time.Minute)
	c := New(filepath.Join(t.TempDir(), "missing"), "test", func(context.Context) (string, bool) { t.Error("missing CLI probed"); return "", false })
	c.Populate(6, time.Hour)
	if c.Get() != "" {
		t.Fatal("missing binary cached a version")
	}
}

func TestPopulateRetriesUntilSuccess(t *testing.T) {
	configureTest(t, nil, 0)
	var calls atomic.Int32
	c := testCache(t, func(context.Context) (string, bool) { n := calls.Add(1); return "3", n == 3 })
	c.Populate(6, 0)
	if c.Get() != "3" || calls.Load() != 3 {
		t.Fatalf("version=%q, probes=%d", c.Get(), calls.Load())
	}
}

func TestCancellationDropsQueuedGet(t *testing.T) {
	cancel := configureTest(t, nil, 0)
	entered := make(chan struct{})
	first := testCache(t, func(ctx context.Context) (string, bool) { close(entered); <-ctx.Done(); return "", false })
	done := make(chan struct{})
	go func() { first.Populate(0, 0); close(done) }()
	await(t, entered)
	var calls atomic.Int32
	queued := testCache(t, func(context.Context) (string, bool) { calls.Add(1); return "2", true })
	queued.Get()
	if !queued.probing.Load() {
		t.Fatal("Get did not queue")
	}
	cancel()
	await(t, done)
	if !waitFor(t, func() bool { return !queued.probing.Load() }) {
		t.Fatal("queued Get did not cancel")
	}
	if calls.Load() != 0 {
		t.Fatal("queued CLI ran after cancellation")
	}
}
