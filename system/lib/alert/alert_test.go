package alert

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/server/config"
)

// captureServer stands in for bff-campaign-service and records the last request.
type captured struct {
	path  string
	auth  string
	ctype string
	body  payload
	hits  int
}

func newCaptureServer(t *testing.T, c *captured) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c.hits++
		c.path = r.URL.Path
		c.auth = r.Header.Get("Authorization")
		c.ctype = r.Header.Get("Content-Type")
		b, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(b, &c.body)
		w.WriteHeader(http.StatusOK)
	}))
}

func TestNotify_PostsToAlertPathWithBearerAndBody(t *testing.T) {
	var c captured
	srv := newCaptureServer(t, &c)
	defer srv.Close()

	cfg := &config.Config{
		// bff base ends in /v1 (OpenAI-compat), like production LLMBaseURL.
		LLMBaseURL: srv.URL + "/v1",
		LLMAPIKey:  "lob_test_key",
		DeviceID:   "dev-123",
	}

	Notify(context.Background(), cfg, "hello world")

	if c.hits != 1 {
		t.Fatalf("want 1 request, got %d", c.hits)
	}
	// LLMBaseURL already ends in /v1, so the endpoint is {base}/alert with no
	// stripping — i.e. /v1/alert (the /api/v1/ai/v1/alert sibling of chat/completions).
	if !strings.HasSuffix(c.path, "/v1/alert") {
		t.Errorf("path = %q, want suffix /v1/alert", c.path)
	}
	if c.auth != "Bearer lob_test_key" {
		t.Errorf("auth = %q, want Bearer lob_test_key", c.auth)
	}
	if c.ctype != "application/json" {
		t.Errorf("content-type = %q, want application/json", c.ctype)
	}
	if c.body.DeviceID != "dev-123" {
		t.Errorf("device_id = %q, want dev-123", c.body.DeviceID)
	}
	if c.body.Type != "ops" {
		t.Errorf("type = %q, want ops", c.body.Type)
	}
	if c.body.Message != "hello world" {
		t.Errorf("message = %q, want hello world", c.body.Message)
	}
	if c.body.TS == 0 {
		t.Errorf("ts not set")
	}
}

func TestNotify_SkipsWhenBaseOrKeyEmpty(t *testing.T) {
	var c captured
	srv := newCaptureServer(t, &c)
	defer srv.Close()

	cases := []*config.Config{
		{LLMBaseURL: "", LLMAPIKey: "k"},
		{LLMBaseURL: srv.URL, LLMAPIKey: ""},
		nil,
	}
	for i, cfg := range cases {
		Notify(context.Background(), cfg, "x")
		if c.hits != 0 {
			t.Fatalf("case %d: expected no request, got %d", i, c.hits)
		}
	}
}

func TestNotify_SkipsWhenAlertsDisabled(t *testing.T) {
	var c captured
	srv := newCaptureServer(t, &c)
	defer srv.Close()

	cfg := &config.Config{LLMBaseURL: srv.URL, LLMAPIKey: "k", AlertsDisabled: true}
	Notify(context.Background(), cfg, "x")
	if c.hits != 0 {
		t.Fatalf("alerts disabled: expected no request, got %d", c.hits)
	}
}

func TestNotify_TruncatesLongMessage(t *testing.T) {
	var c captured
	srv := newCaptureServer(t, &c)
	defer srv.Close()

	cfg := &config.Config{LLMBaseURL: srv.URL, LLMAPIKey: "k", DeviceID: "d"}
	long := strings.Repeat("a", maxMessageLen+500)
	Notify(context.Background(), cfg, long)

	if got := len(c.body.Message); got != maxMessageLen {
		t.Errorf("message len = %d, want %d (truncated)", got, maxMessageLen)
	}
}

func TestCompose_IncludesTitleAndPreamble(t *testing.T) {
	cfg := &config.Config{DeviceID: "dev-9"}
	out := Compose(cfg, "🟢 Title", "detail-line")
	if !strings.HasPrefix(out, "🟢 Title\n") {
		t.Errorf("compose should start with the title line, got %q", out)
	}
	if !strings.Contains(out, "dev-9") {
		t.Errorf("compose should embed the device label, got %q", out)
	}
	if !strings.Contains(out, "detail-line") {
		t.Errorf("compose should append the detail, got %q", out)
	}
}

// A BYO-LLM device (llm_base_url on Ollama) keeps its ops alerts on the
// Autonomous backend once backend_base_url / backend_api_key are set.
func TestNotifyUsesBackendOverride(t *testing.T) {
	var c captured
	srv := newCaptureServer(t, &c)
	defer srv.Close()
	cfg := &config.Config{
		DeviceID:       "lamp-1",
		LLMBaseURL:     "http://127.0.0.1:1/v1", // local model server, must not be hit
		LLMAPIKey:      "ollama",
		BackendBaseURL: srv.URL,
		BackendAPIKey:  "backend-key",
	}
	Notify(context.Background(), cfg, "hello")
	if c.hits != 1 {
		t.Fatalf("hits = %d, want 1 on the backend server", c.hits)
	}
	if c.auth != "Bearer backend-key" {
		t.Fatalf("auth = %q, want backend key", c.auth)
	}
}
