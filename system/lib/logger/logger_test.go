package logger

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestLevelFromEnv(t *testing.T) {
	tests := []struct {
		name  string
		value string
		want  slog.Level
	}{
		{name: "debug", value: "DEBUG", want: slog.LevelDebug},
		{name: "case and whitespace ignored", value: " warning ", want: slog.LevelWarn},
		{name: "error", value: "ERROR", want: slog.LevelError},
		{name: "invalid uses default", value: "TRACE", want: slog.LevelInfo},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv("HAL_LOG_LEVEL", tt.value)
			if got := levelFromEnv(); got != tt.want {
				t.Fatalf("levelFromEnv() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestGELFHandlerDropsWhenBoundedQueueIsFull(t *testing.T) {
	started := make(chan struct{}, 1)
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case started <- struct{}{}:
		default:
		}
		select {
		case <-release:
		case <-r.Context().Done():
		}
	}))
	defer server.Close()

	oldURL, oldUsername, oldPassword := gelfURL, gelfUsername, gelfPassword
	gelfURL, gelfUsername, gelfPassword = server.URL, "", ""
	defer func() { gelfURL, gelfUsername, gelfPassword = oldURL, oldUsername, oldPassword }()

	h := newGELFHandler(slog.LevelInfo, "test")
	defer h.sink.load().close()

	if err := h.Handle(context.Background(), slog.NewRecord(time.Now(), slog.LevelInfo, "first", 0)); err != nil {
		t.Fatalf("first Handle() error = %v", err)
	}
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("GELF worker did not start request")
	}

	for i := 0; i <= gelfQueueSize; i++ {
		if err := h.Handle(context.Background(), slog.NewRecord(time.Now(), slog.LevelInfo, "queued", 0)); err != nil {
			t.Fatalf("Handle() error = %v", err)
		}
	}
	if h.sink.load().dropped.Load() == 0 {
		t.Fatal("expected at least one GELF record to be dropped when queue is full")
	}

	start := time.Now()
	if err := h.Handle(context.Background(), slog.NewRecord(time.Now(), slog.LevelInfo, "overflow", 0)); err != nil {
		t.Fatalf("overflow Handle() error = %v", err)
	}
	if elapsed := time.Since(start); elapsed > 100*time.Millisecond {
		t.Fatalf("overflow Handle() blocked for %s", elapsed)
	}
	close(release)
}

func TestGELFSenderFlushesQueuedRecordsOnClose(t *testing.T) {
	var received atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		received.Add(1)
		w.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()

	oldURL, oldUsername, oldPassword := gelfURL, gelfUsername, gelfPassword
	gelfURL, gelfUsername, gelfPassword = server.URL, "", ""
	defer func() { gelfURL, gelfUsername, gelfPassword = oldURL, oldUsername, oldPassword }()

	h := newGELFHandler(slog.LevelInfo, "test")
	for i := 0; i < 3; i++ {
		if err := h.Handle(context.Background(), slog.NewRecord(time.Now(), slog.LevelInfo, "flush", 0)); err != nil {
			t.Fatalf("Handle() error = %v", err)
		}
	}
	h.sink.load().close()

	if got := received.Load(); got != 3 {
		t.Fatalf("GELF records sent before close = %d, want 3", got)
	}
}

// capturedGELF is one request a test collector received.
type capturedGELF struct {
	path          string
	authorization string
	contentType   string
	basicUser     string
	body          string
}

// gelfCollector records every request for assertions.
type gelfCollector struct {
	*httptest.Server
	mu   sync.Mutex
	reqs []capturedGELF
}

func newGELFCollector(t *testing.T) *gelfCollector {
	t.Helper()
	c := &gelfCollector{}
	c.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		user, _, _ := r.BasicAuth()
		c.mu.Lock()
		c.reqs = append(c.reqs, capturedGELF{
			path:          r.URL.Path,
			authorization: r.Header.Get("Authorization"),
			contentType:   r.Header.Get("Content-Type"),
			basicUser:     user,
			body:          string(body),
		})
		c.mu.Unlock()
		w.WriteHeader(http.StatusAccepted)
	}))
	t.Cleanup(c.Close)
	return c
}

func (c *gelfCollector) received() []capturedGELF {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]capturedGELF(nil), c.reqs...)
}

// initTestLogger runs Init with GELF_URL=gelfEnvURL ("" = relay) and returns a flush-and-restore func.
func initTestLogger(t *testing.T, gelfEnvURL string) func() {
	t.Helper()
	t.Setenv("GELF_URL", gelfEnvURL)
	t.Setenv("GELF_USERNAME", "graylog-user")
	t.Setenv("GELF_PASSWORD", "graylog-pass")
	prev := slog.Default()
	oldURL, oldUsername, oldPassword := gelfURL, gelfUsername, gelfPassword
	cleanup := Init(filepath.Join(t.TempDir(), "os-server.log"))
	return func() {
		cleanup()
		slog.SetDefault(prev)
		gelfURL, gelfUsername, gelfPassword = oldURL, oldUsername, oldPassword
	}
}

func TestGELFRelayShipsToCloudAPIWithDeviceKey(t *testing.T) {
	collector := newGELFCollector(t)
	done := initTestLogger(t, "")

	EnableGELFRelay(collector.URL+"/api/v1/ai/v1/", "lobster-key")
	slog.Info("relayed record")
	done()

	got := collector.received()
	if len(got) != 1 {
		t.Fatalf("relay requests = %d, want 1", len(got))
	}
	r := got[0]
	if r.path != "/api/v1/ai/v1/logs/gelf" {
		t.Errorf("path = %q, want /api/v1/ai/v1/logs/gelf", r.path)
	}
	if r.authorization != "Bearer lobster-key" {
		t.Errorf("Authorization = %q, want the device key as a Bearer token", r.authorization)
	}
	if r.basicUser != "" {
		t.Errorf("relay sent basic auth user %q; the Graylog credential must never leave the device", r.basicUser)
	}
	if r.contentType != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", r.contentType)
	}
	if !strings.Contains(r.body, `"short_message":"relayed record"`) {
		t.Errorf("body = %s, want the GELF record", r.body)
	}
}

func TestGELFRelayIgnoredWhenGELFURLSet(t *testing.T) {
	direct := newGELFCollector(t)
	relay := newGELFCollector(t)
	done := initTestLogger(t, direct.URL)

	EnableGELFRelay(relay.URL, "lobster-key")
	slog.Info("direct record")
	done()

	if got := relay.received(); len(got) != 0 {
		t.Fatalf("relay requests = %d, want 0: GELF_URL must win over the relay", len(got))
	}
	got := direct.received()
	if len(got) != 1 {
		t.Fatalf("direct requests = %d, want 1", len(got))
	}
	if got[0].basicUser != "graylog-user" {
		t.Errorf("direct basic auth user = %q, want graylog-user", got[0].basicUser)
	}
}

func TestGELFHandlerDormantUntilRelayArmed(t *testing.T) {
	done := initTestLogger(t, "")
	defer done()

	if activeGELF == nil {
		t.Fatal("Init attached no GELF handler, so EnableGELFRelay would have nothing to arm")
	}
	if activeGELF.Enabled(context.Background(), slog.LevelError) {
		t.Fatal("dormant GELF handler reports Enabled; records would be serialized for nowhere")
	}
	if activeGELF.sink.load() != nil {
		t.Fatal("dormant GELF handler already has a sender (and its goroutine)")
	}
}

func TestGELFRelayArmsLoggersCreatedBeforeIt(t *testing.T) {
	collector := newGELFCollector(t)
	done := initTestLogger(t, "")

	// Package-level loggers hold handler copies made before the relay was configured.
	early := slog.Default().With("component", "early")
	EnableGELFRelay(collector.URL, "lobster-key")
	early.Info("from early logger")
	done()

	got := collector.received()
	if len(got) != 1 {
		t.Fatalf("relay requests = %d, want 1 from the logger created before arming", len(got))
	}
	if !strings.Contains(got[0].body, `"_component":"early"`) {
		t.Errorf("body = %s, want the early logger's attrs", got[0].body)
	}
}

func TestGELFRelayStaysDormantWithoutCredentials(t *testing.T) {
	cases := []struct {
		name, base, key string
	}{
		{name: "no base URL", base: "", key: "lobster-key"},
		{name: "blank base URL", base: "   ", key: "lobster-key"},
		{name: "no key", base: "https://device-api.autonomous.ai/api/v1/ai/v1", key: ""},
		{name: "blank key", base: "https://device-api.autonomous.ai/api/v1/ai/v1", key: "  "},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			done := initTestLogger(t, "")
			defer done()

			EnableGELFRelay(tc.base, tc.key)
			if activeGELF.Enabled(context.Background(), slog.LevelError) {
				t.Fatal("relay armed without a usable base URL and key")
			}
		})
	}
}

func TestGELFRelaySecondCallKeepsFirstTarget(t *testing.T) {
	first := newGELFCollector(t)
	second := newGELFCollector(t)
	done := initTestLogger(t, "")

	EnableGELFRelay(first.URL, "key-1")
	EnableGELFRelay(second.URL, "key-2")
	slog.Info("once")
	done()

	if got := first.received(); len(got) != 1 {
		t.Fatalf("first target requests = %d, want 1", len(got))
	}
	if got := second.received(); len(got) != 0 {
		t.Fatalf("second target requests = %d, want 0: a repeat call must not re-arm", len(got))
	}
}
