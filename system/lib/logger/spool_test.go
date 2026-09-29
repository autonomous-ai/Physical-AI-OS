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
	mu       sync.Mutex
	bodies   []string
}

func newFlakyCollector(t *testing.T) *flakyCollector {
	t.Helper()
	c := &flakyCollector{}
	c.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c.attempts.Add(1)
		body, _ := io.ReadAll(r.Body)
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

	got := collector.accepted()
	if len(got) != 5 {
		t.Fatalf("accepted = %d, want exactly 5 (no duplicates)", len(got))
	}
	for i, b := range got {
		if !strings.Contains(b, fmt.Sprintf(`"record-%d"`, i)) {
			t.Fatalf("record %d = %s, want record-%d: replay must keep order", i, b, i)
		}
	}
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
