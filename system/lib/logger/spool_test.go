package logger

import (
	"bytes"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// shortReplayTiming makes replay and retry fast enough for a unit test.
func shortReplayTiming(t *testing.T) {
	t.Helper()
	replay, rmin, rmax, check := gelfReplayInterval, gelfRetryMin, gelfRetryMax, gelfSpoolCheckInterval
	gelfReplayInterval, gelfRetryMin, gelfRetryMax, gelfSpoolCheckInterval =
		time.Millisecond, 10*time.Millisecond, 40*time.Millisecond, 20*time.Millisecond
	t.Cleanup(func() {
		gelfReplayInterval, gelfRetryMin, gelfRetryMax, gelfSpoolCheckInterval = replay, rmin, rmax, check
	})
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatal("condition not met within 3s")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// flakyCollector rejects every record with 503 until up() is called, then
// accepts and keeps them in arrival order.
type flakyCollector struct {
	*httptest.Server
	ok       atomic.Bool
	attempts atomic.Int32
	// hold, when set, runs before the collector answers its first request.
	hold   atomic.Pointer[func()]
	mu     sync.Mutex
	bodies []string
}

func newFlakyCollector(t *testing.T) *flakyCollector {
	t.Helper()
	c := &flakyCollector{}
	c.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		first := c.attempts.Add(1) == 1
		body, _ := io.ReadAll(r.Body)
		if hold := c.hold.Load(); first && hold != nil {
			(*hold)()
		}
		if !c.ok.Load() {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		c.mu.Lock()
		c.bodies = append(c.bodies, string(body))
		c.mu.Unlock()
		w.WriteHeader(http.StatusAccepted)
	}))
	t.Cleanup(c.Close)
	return c
}

func (c *flakyCollector) accepted() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]string(nil), c.bodies...)
}

// wantRecordsInOrder checks the collector accepted record-0..record-(n-1)
// exactly once each, in that order.
func wantRecordsInOrder(t *testing.T, got []string, n int) {
	t.Helper()
	if len(got) != n {
		t.Fatalf("accepted = %d, want exactly %d (no duplicates)", len(got), n)
	}
	for i, b := range got {
		if !strings.Contains(b, fmt.Sprintf(`"record-%d"`, i)) {
			t.Fatalf("record %d = %s, want record-%d: replay must keep order", i, b, i)
		}
	}
}

func TestGELFSpoolKeepsSetupLogsUntilTheRelayArms(t *testing.T) {
	shortReplayTiming(t)
	collector := newGELFCollector(t)
	done := initTestLogger(t, "")
	dir := t.TempDir()
	EnableGELFSpool(dir, "os-server")

	// First setup: no key and no internet yet, so nothing can ship.
	slog.Info("setup: wifi join failed", "component", "device")
	if got := collector.received(); len(got) != 0 {
		t.Fatalf("requests before arming = %d, want 0", len(got))
	}

	// A later setup succeeds: the device id and key arrive together.
	SetGELFHost("device-123")
	EnableGELFRelay(collector.URL, "lobster-key")
	waitFor(t, func() bool { return len(collector.received()) == 1 })
	done()

	body := collector.received()[0].body
	for _, want := range []string{`"short_message":"setup: wifi join failed"`, `"_spooled":"true"`, `"host":"device-123"`, `"_component":"device"`} {
		if !strings.Contains(body, want) {
			t.Errorf("replayed body = %s, missing %s", body, want)
		}
	}
	if fileHasData(filepath.Join(dir, "os-server.jsonl")) || fileHasData(filepath.Join(dir, "os-server.replay.jsonl")) {
		t.Error("spool not empty after a successful replay")
	}
}

func TestGELFSpoolReplaysInOrderAfterTheCollectorRecovers(t *testing.T) {
	shortReplayTiming(t)
	collector := newFlakyCollector(t)
	done := initTestLogger(t, "")
	EnableGELFSpool(t.TempDir(), "os-server")
	EnableGELFRelay(collector.URL, "lobster-key")

	for i := 0; i < 5; i++ {
		slog.Info(fmt.Sprintf("record-%d", i))
	}
	waitFor(t, func() bool { return collector.attempts.Load() >= 2 }) // failures happened
	collector.ok.Store(true)
	waitFor(t, func() bool { return len(collector.accepted()) >= 5 })
	done()

	wantRecordsInOrder(t, collector.accepted(), 5)
}

// A record still queued when a send fails is older than anything logged after
// the failure, so it must be replayed first.
func TestGELFSpoolReplaysQueuedRecordsBeforeLaterOnes(t *testing.T) {
	shortReplayTiming(t)
	collector := newFlakyCollector(t)
	sending, fail := make(chan struct{}), make(chan struct{})
	hold := func() { close(sending); <-fail }
	collector.hold.Store(&hold)
	done := initTestLogger(t, "")
	EnableGELFSpool(t.TempDir(), "os-server")
	EnableGELFRelay(collector.URL, "lobster-key")

	slog.Info("record-0")
	<-sending
	slog.Info("record-1") // queued behind the send in flight
	close(fail)
	waitFor(t, func() bool { return collector.attempts.Load() >= 2 }) // replay has started
	for i := 2; i < 5; i++ {
		slog.Info(fmt.Sprintf("record-%d", i)) // logged after the failure
	}
	collector.ok.Store(true)
	waitFor(t, func() bool { return len(collector.accepted()) >= 5 })
	done()

	wantRecordsInOrder(t, collector.accepted(), 5)
}

func TestGELFSpoolIsBoundedAndDropsTheOldest(t *testing.T) {
	dir := t.TempDir()
	s, err := newGELFSpool(dir, "os-server", 1000)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 200; i++ {
		s.append([]byte(fmt.Sprintf(`{"short_message":"line-%03d"}`, i)))
	}
	var total int64
	for _, name := range []string{"os-server.jsonl", "os-server.1.jsonl"} {
		if fi, err := os.Stat(filepath.Join(dir, name)); err == nil {
			total += fi.Size()
		}
	}
	if total > 1000 {
		t.Fatalf("spool holds %d bytes, want <= 1000", total)
	}
	if err := s.take(); err != nil {
		t.Fatal(err)
	}
	lines, err := s.replayLines()
	if err != nil {
		t.Fatal(err)
	}
	if len(lines) == 0 || !bytes.Contains(lines[len(lines)-1], []byte("line-199")) {
		t.Fatalf("newest record missing from replay (%d lines)", len(lines))
	}
	if bytes.Contains(lines[0], []byte("line-000")) {
		t.Fatal("oldest record kept; the bound must drop the oldest first")
	}
}

func TestGELFSendClassifiesCollectorAnswers(t *testing.T) {
	cases := map[int]sendResult{
		http.StatusAccepted:              sendOK,
		http.StatusOK:                    sendOK,
		http.StatusUnauthorized:          sendRetry, // key not accepted yet: keep for a later re-target
		http.StatusTooManyRequests:       sendRetry,
		http.StatusServiceUnavailable:    sendRetry,
		http.StatusRequestEntityTooLarge: sendDrop,
		http.StatusBadRequest:            sendDrop,
	}
	for code, want := range cases {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(code) }))
		s := newGELFSender(server.Client(), server.URL, nil)
		if got := s.send([]byte(`{}`)); got != want {
			t.Errorf("HTTP %d -> %v, want %v", code, got, want)
		}
		s.close()
		server.Close()
	}
}

func TestGELFIdentityReachesLoggersBuiltBeforeConfig(t *testing.T) {
	collector := newGELFCollector(t)
	done := initTestLogger(t, "")

	early := slog.Default().With("component", "early")
	SetGELFHost("device-123")
	SetGELFDeviceType("intern-v2")
	SetGELFServiceName("bootstrap")
	EnableGELFRelay(collector.URL, "lobster-key")
	early.Info("from early logger")
	done()

	got := collector.received()
	if len(got) != 1 {
		t.Fatalf("requests = %d, want 1", len(got))
	}
	for _, want := range []string{`"host":"device-123"`, `"_device_type":"intern-v2"`, `"_service_name":"bootstrap"`} {
		if !strings.Contains(got[0].body, want) {
			t.Errorf("body = %s, missing %s", got[0].body, want)
		}
	}
}

func TestGELFRelayRetargetDoesNotReplayTheSpoolTwice(t *testing.T) {
	shortReplayTiming(t)
	// The first target accepts slowly, so its sender is mid-replay when the
	// relay re-targets. A request cancelled by the client is not accepted.
	var mu sync.Mutex
	var accepted []string
	accept := func(body []byte) {
		mu.Lock()
		accepted = append(accepted, string(body))
		mu.Unlock()
	}
	count := func() int {
		mu.Lock()
		defer mu.Unlock()
		return len(accepted)
	}
	slow := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		select {
		case <-time.After(30 * time.Millisecond):
			accept(body)
			w.WriteHeader(http.StatusAccepted)
		case <-r.Context().Done():
		}
	}))
	t.Cleanup(slow.Close)
	fast := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		accept(body)
		w.WriteHeader(http.StatusAccepted)
	}))
	t.Cleanup(fast.Close)

	done := initTestLogger(t, "")
	EnableGELFSpool(t.TempDir(), "os-server")
	for i := 0; i < 10; i++ {
		slog.Info(fmt.Sprintf("record-%d", i)) // spooled: relay not armed yet
	}
	EnableGELFRelay(slow.URL, "key-1")
	waitFor(t, func() bool { return count() >= 2 }) // old sender is mid-replay
	EnableGELFRelay(fast.URL, "key-2")
	waitFor(t, func() bool { return count() >= 10 })
	time.Sleep(150 * time.Millisecond) // room for any duplicate to arrive
	done()

	mu.Lock()
	defer mu.Unlock()
	if len(accepted) != 10 {
		t.Fatalf("accepted %d records across both targets, want exactly 10 (no duplicates)", len(accepted))
	}
	for i, b := range accepted {
		if !strings.Contains(b, fmt.Sprintf(`"record-%d"`, i)) {
			t.Fatalf("record %d = %s, want record-%d: replay must keep order", i, b, i)
		}
	}
}

func TestGELFReplayNeverShipsAnotherDevicesRecords(t *testing.T) {
	shortReplayTiming(t)
	collector := newGELFCollector(t)
	dir := t.TempDir()

	// Previous owner: records logged while the relay could not deliver stay
	// in the spool across the reboot that follows a factory reset.
	done := initTestLogger(t, "")
	EnableGELFSpool(dir, "os-server")
	SetGELFHost("previous-device")
	slog.Info("previous owner's speech")
	done()

	// Next boot: the new owner sets the device up.
	done = initTestLogger(t, "")
	EnableGELFSpool(dir, "os-server")
	slog.Info("setup: wifi joined")
	SetGELFHost("new-device")
	EnableGELFRelay(collector.URL, "new-owner-key")
	waitFor(t, func() bool { return len(collector.received()) >= 1 })
	time.Sleep(100 * time.Millisecond)
	done()

	got := collector.received()
	if len(got) != 1 {
		t.Fatalf("requests = %d, want 1 (only this setup's record)", len(got))
	}
	if strings.Contains(got[0].body, "previous owner") || !strings.Contains(got[0].body, `"host":"new-device"`) {
		t.Errorf("shipped %s, want only this device's setup record", got[0].body)
	}
}
