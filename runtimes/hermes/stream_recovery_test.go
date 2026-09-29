package hermes

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/monitor"
)

type recoveryTransport func(*http.Request) (*http.Response, error)

func (f recoveryTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func waitRecovery(t *testing.T, done func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if done() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("stream recovery timed out")
}

func TestHealthRecoveryDrainsUnsentWebChatsSeparately(t *testing.T) {
	requests := make(chan string, 4)
	release := make(chan struct{})
	defer close(release)
	s := &HermesService{monitorBus: monitor.ProvideBus()}
	s.httpClient = &http.Client{Transport: recoveryTransport(func(r *http.Request) (*http.Response, error) {
		var body streamRequest
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			return nil, err
		}
		requests <- body.Input.(string)
		<-release
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader("event: response.completed\ndata: {\"response\":{\"id\":\"resp\",\"output\":[]}}\n\n"))}, nil
	})}
	for _, msg := range []string{"first", "second"} {
		s.QueuePendingEvent("web_chat", msg, nil, msg)
	}
	s.DrainPendingEvents()
	if len(s.pendingEvents) != 2 {
		t.Fatal("offline drain removed unsent requests")
	}
	s.transitionReady(true)
	select {
	case msg := <-requests:
		if !strings.Contains(msg, "first") || strings.Contains(msg, "second") {
			t.Fatalf("merged/wrong input %q", msg)
		}
	case <-time.After(time.Second):
		t.Fatal("health recovery did not drain")
	}
	s.SetBusy(false)
	s.DrainPendingEvents()
	select {
	case msg := <-requests:
		t.Fatalf("second request ran before first ended: %q", msg)
	default:
	}
	release <- struct{}{}
	select {
	case msg := <-requests:
		if !strings.Contains(msg, "second") {
			t.Fatalf("wrong second input %q", msg)
		}
	case <-time.After(time.Second):
		t.Fatal("second request stranded")
	}
	release <- struct{}{}
	waitRecovery(t, func() bool { return s.inFlightStreams.Load() == 0 })
	s.DrainPendingEvents()
	select {
	case msg := <-requests:
		t.Fatalf("duplicate request: %q", msg)
	default:
	}
}

func TestTruncatedSSEErrorsWithoutReplayingAttemptedPost(t *testing.T) {
	var calls atomic.Int64
	var errorsSeen atomic.Int64
	s := &HermesService{monitorBus: monitor.ProvideBus()}
	s.ready.Store(true)
	s.handler = func(_ context.Context, evt domain.WSEvent) error {
		if strings.Contains(string(evt.Payload), `"phase":"error"`) {
			errorsSeen.Add(1)
		}
		return nil
	}
	s.httpClient = &http.Client{Transport: recoveryTransport(func(*http.Request) (*http.Response, error) {
		calls.Add(1)
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader("event: response.created\ndata: {\"response\":{\"id\":\"resp\"}}\n\n"))}, nil
	})}
	if _, err := s.SendChatMessage("do it once"); err != nil {
		t.Fatal(err)
	}
	waitRecovery(t, func() bool { return s.inFlightStreams.Load() == 0 })
	s.DrainPendingEvents()
	if calls.Load() != 1 || errorsSeen.Load() != 1 || s.IsBusy() {
		t.Fatalf("calls=%d errors=%d busy=%v", calls.Load(), errorsSeen.Load(), s.IsBusy())
	}
}

func TestOneStreamFailureDoesNotClearAnotherStream(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	defer close(release)
	s := &HermesService{monitorBus: monitor.ProvideBus()}
	s.ready.Store(true)
	s.httpClient = &http.Client{Transport: recoveryTransport(func(r *http.Request) (*http.Response, error) {
		var body streamRequest
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			return nil, err
		}
		if body.Input == "first" {
			close(started)
			<-release
			return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader("event: response.completed\ndata: {}\n\n"))}, nil
		}
		return &http.Response{StatusCode: 503, Header: make(http.Header), Body: io.NopCloser(strings.NewReader("unavailable"))}, nil
	})}
	if _, err := s.SendChatMessage("first"); err != nil {
		t.Fatal(err)
	}
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("first stream did not start")
	}
	if _, err := s.SendChatMessage("second"); err != nil {
		t.Fatal(err)
	}
	waitRecovery(t, func() bool { return s.inFlightStreams.Load() == 1 })
	s.busySince.Store(time.Now().Add(-10 * time.Minute).UnixMilli())
	s.SetBusy(false)
	if !s.IsBusy() || !s.activeTurn.Load() {
		t.Fatal("failed second stream cleared running first stream")
	}
	release <- struct{}{}
	waitRecovery(t, func() bool { return !s.IsBusy() })
}

func TestSSETerminalStopsBeforeTrailingData(t *testing.T) {
	s := &HermesService{}
	emitted := 0
	res, err := s.readSSE(context.Background(), "own-run", strings.NewReader("event: response.completed\ndata: {\"response\":{\"output\":[]}}\n\nevent: response.output_text.delta\ndata: {\"delta\":\"late\"}\n\n"), func(evt domain.WSEvent) {
		emitted++
		if !strings.Contains(string(evt.Payload), "own-run") {
			t.Errorf("wrong correlation %s", evt.Payload)
		}
	})
	if err != nil || !res.Terminal || emitted != 2 {
		t.Fatalf("result=%+v err=%v events=%d", res, err, emitted)
	}
}

// Readiness can drop after a drain detached A/B and a newer C was queued.
func TestNotReadyReplayPreservesDetachedOrder(t *testing.T) {
	s := &HermesService{monitorBus: monitor.ProvideBus()}
	a := pendingEvent{eventType: "web_chat", msg: "A", fixedRunID: "run-A"}
	b := pendingEvent{eventType: "web_chat", msg: "B", fixedRunID: "run-B"}
	s.QueuePendingEvent("web_chat", "C", nil, "run-C")
	s.restoreUnsent([]pendingEvent{b})
	s.sendOnePending(a)
	s.pendingEventsMu.Lock()
	defer s.pendingEventsMu.Unlock()
	if len(s.pendingEvents) != 3 {
		t.Fatalf("queue = %+v", s.pendingEvents)
	}
	for i, want := range []string{"run-A", "run-B", "run-C"} {
		if s.pendingEvents[i].fixedRunID != want {
			t.Fatalf("queue[%d] = %+v; want %s", i, s.pendingEvents[i], want)
		}
	}
}

func TestHarnessChatsReplayAsStandaloneTurns(t *testing.T) {
	for _, kind := range []string{"web_chat", "mqtt_chat", "voice_followup"} {
		if !standaloneDrain(pendingEvent{eventType: kind}) {
			t.Errorf("%s must retain its own response run", kind)
		}
	}
}
