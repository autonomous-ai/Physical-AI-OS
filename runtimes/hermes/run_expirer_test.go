package hermes

import (
	"context"
	"encoding/json"
	"fmt"
	"go.autonomous.ai/os/system/domain"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestRunExpiryRejectsNewerOwner(t *testing.T) {
	for _, active := range []*managedTurn{
		nil,
		{owner: "new", requests: []managedChat{{runID: "new"}}},
		{owner: "old", requests: []managedChat{{runID: "old"}, {runID: "new"}}},
	} {
		called := false
		if active != nil {
			active.cancel = func() { called = true }
		}
		if err := expireManagedOwner(active, runExpiry{ctx: context.Background(), runID: "old", reason: "deadline"}); err == nil || called {
			t.Fatal("stale deadline cancelled newer work")
		}
	}
}

func TestRunExpiryStopsNativeRunAndEmitsError(t *testing.T) {
	oldURL := BaseURL
	var stopped atomic.Bool
	var creates atomic.Int32
	steered := make(chan struct{}, 1)
	connected := make(chan struct{}, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/runs":
			creates.Add(1)
			fmt.Fprint(w, `{"run_id":"native-expiry"}`)
		case strings.HasSuffix(r.URL.Path, "/steer"):
			steered <- struct{}{}
			fmt.Fprint(w, `{"accepted":true}`)
		case strings.HasSuffix(r.URL.Path, "/stop"):
			stopped.Store(true)
			fmt.Fprint(w, `{}`)
		case strings.HasSuffix(r.URL.Path, "/events"):
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(200)
			w.(http.Flusher).Flush()
			connected <- struct{}{}
			<-r.Context().Done()
		default:
			status := "running"
			if stopped.Load() {
				status = "completed"
			}
			fmt.Fprintf(w, `{"run_id":"native-expiry","status":%q,"session_id":"session-expiry","pending_steer":"second"}`, status)
		}
	}))
	BaseURL = server.URL
	ctx, cancel := context.WithCancel(context.Background())
	defer func() { cancel(); server.Close(); BaseURL = oldURL }()
	events := make(chan domain.WSEvent, 30)
	s := &HermesService{httpClient: server.Client(), runtimeCtx: ctx, silentRuns: map[string]bool{}, handler: func(_ context.Context, e domain.WSEvent) error { events <- e; return nil }}
	s.inFlightStreams.Store(2)
	s.enqueueManagedRun("device-expiry", streamRequest{Input: "prepare", Conversation: "expiry"}, "user")
	select {
	case <-connected:
	case <-time.After(3 * time.Second):
		t.Fatal("reader did not connect")
	}
	s.enqueueManagedRun("device-expiry-new", streamRequest{Input: "second", Conversation: "expiry"}, "user")
	select {
	case <-steered:
	case <-time.After(3 * time.Second):
		t.Fatal("new request did not steer")
	}
	deadline, done := context.WithTimeout(context.Background(), 3*time.Second)
	defer done()
	if err := s.ExpireRun(deadline, "device-expiry", "expired"); err == nil {
		t.Fatal("wrong owner accepted")
	}
	if stopped.Load() {
		t.Fatal("wrong owner stopped remote run")
	}
	if err := s.ExpireRun(deadline, "device-expiry-new", "Preparation wait expired; task was not sent."); err != nil {
		t.Fatal(err)
	}
	for {
		select {
		case event := <-events:
			var payload struct {
				RunID  string `json:"runId"`
				Stream string `json:"stream"`
				Data   struct {
					Phase string `json:"phase"`
					Error string `json:"error"`
				} `json:"data"`
			}
			json.Unmarshal(event.Payload, &payload)
			if payload.Stream != "lifecycle" || payload.Data.Phase == "start" {
				continue
			}
			if payload.Data.Phase != "error" || !strings.Contains(payload.Data.Error, "Preparation wait expired") || !stopped.Load() {
				t.Fatalf("wrong terminal: %s", event.Payload)
			}
			if payload.RunID != "device-expiry-new" {
				continue
			}
			if creates.Load() != 1 {
				t.Fatal("expired steering suffix was replayed")
			}
			return
		case <-deadline.Done():
			t.Fatal("expiry did not terminate run")
		}
	}
}
