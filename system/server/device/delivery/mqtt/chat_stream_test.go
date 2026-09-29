package mqtthandler

import (
	"encoding/json"
	"sync"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/monitor"
	agenthttp "go.autonomous.ai/os/system/server/agent/delivery/http"
	"go.autonomous.ai/os/system/server/config"
)

// newTestStream builds a stream whose transport captures payloads.
func newTestStream() (*ChatStream, func() []domain.MQTTChatEventData) {
	var mu sync.Mutex
	var sent []domain.MQTTChatEventData

	s := &ChatStream{
		cfg:  &config.Config{DeviceID: "dev1", FDChannel: "fd/dev1"},
		runs: map[string]*trackedRun{},
	}
	s.publish = func(body []byte) error {
		var resp struct {
			Kind string                   `json:"kind"`
			Data domain.MQTTChatEventData `json:"data"`
		}
		if err := json.Unmarshal(body, &resp); err != nil {
			return err
		}
		mu.Lock()
		defer mu.Unlock()
		sent = append(sent, resp.Data)
		return nil
	}

	return s, func() []domain.MQTTChatEventData {
		mu.Lock()
		defer mu.Unlock()
		out := make([]domain.MQTTChatEventData, len(sent))
		copy(out, sent)
		return out
	}
}

// The bus carries every turn on the device, including voice ones.
func TestChatStreamIgnoresUntrackedRuns(t *testing.T) {
	s, sent := newTestStream()

	s.handle(domain.MonitorEvent{Type: "chat_response", RunID: "not-mine", Summary: "hi"})
	s.handle(domain.MonitorEvent{Type: "assistant_delta", RunID: "", Summary: "orphan"})
	s.flushAll()

	if got := sent(); len(got) != 0 {
		t.Fatalf("published %d events for untracked runs: %+v", len(got), got)
	}
}

// Deltas are accumulated, not forwarded 1:1.
func TestChatStreamCoalescesDeltas(t *testing.T) {
	s, sent := newTestStream()
	s.Track("run1", "sess1")

	for _, chunk := range []string{"Hello", " there", ", Leo"} {
		s.handle(domain.MonitorEvent{Type: "assistant_delta", RunID: "run1", Summary: chunk})
	}
	if got := sent(); len(got) != 0 {
		t.Fatalf("deltas published before a flush: %+v", got)
	}

	s.flushAll()

	got := sent()
	if len(got) != 1 {
		t.Fatalf("want 1 coalesced event, got %d: %+v", len(got), got)
	}
	if got[0].Event.Summary != "Hello there, Leo" {
		t.Errorf("text = %q, want the three chunks joined", got[0].Event.Summary)
	}
	if got[0].SessionID != "sess1" {
		t.Errorf("session = %q, want it echoed on every event", got[0].SessionID)
	}
	if got[0].RunID != "run1" {
		t.Errorf("run id = %q", got[0].RunID)
	}
	// A coalesced event is not any one of the chunks it was built from, so it
	// must not reuse their id.
	if got[0].Event.ID != "" {
		t.Errorf("coalesced event kept id %q", got[0].Event.ID)
	}
}

// Pending delta text is flushed before any other event.
func TestChatStreamFlushesPendingBeforeOtherEvents(t *testing.T) {
	s, sent := newTestStream()
	s.Track("run1", "sess1")

	s.handle(domain.MonitorEvent{Type: "assistant_delta", RunID: "run1", Summary: "Taking a photo"})
	s.handle(domain.MonitorEvent{Type: "tool_call", RunID: "run1", Summary: "Tool Bash"})

	got := sent()
	if len(got) != 2 {
		t.Fatalf("want 2 events, got %d: %+v", len(got), got)
	}
	if got[0].Event.Type != "assistant_delta" || got[0].Event.Summary != "Taking a photo" {
		t.Errorf("first event = %+v, want the buffered text", got[0].Event)
	}
	if got[1].Event.Type != "tool_call" {
		t.Errorf("second event = %+v, want the tool call", got[1].Event)
	}
}

// A terminal chat_response is published, then the run stops being mirrored.
func TestChatStreamStopsAtTerminalEvent(t *testing.T) {
	s, sent := newTestStream()
	s.Track("run1", "sess1")

	s.handle(domain.MonitorEvent{Type: "chat_response", State: "final", RunID: "run1", Summary: "done"})
	s.handle(domain.MonitorEvent{Type: "assistant_delta", RunID: "run1", Summary: "late"})
	s.flushAll()

	got := sent()
	if len(got) != 1 {
		t.Fatalf("want only the terminal event, got %d: %+v", len(got), got)
	}
	if got[0].Event.Type != "chat_response" {
		t.Errorf("event = %+v", got[0].Event)
	}

	s.mu.Lock()
	_, still := s.runs["run1"]
	s.mu.Unlock()
	if still {
		t.Error("run still tracked after its terminal event")
	}
}

// Only a terminal chat_response state ends the mirror.
func TestChatStreamChatResponseNotTerminalUntilFinalState(t *testing.T) {
	s, sent := newTestStream()
	s.Track("run1", "sess1")

	s.handle(domain.MonitorEvent{Type: "chat_response", State: "delta", RunID: "run1", Summary: "Why"})
	s.handle(domain.MonitorEvent{Type: "chat_response", State: "delta", RunID: "run1", Summary: "Why don't"})
	s.handle(domain.MonitorEvent{Type: "chat_response", State: "final", RunID: "run1", Summary: "Why don't scientists trust atoms?"})

	s.mu.Lock()
	_, still := s.runs["run1"]
	s.mu.Unlock()
	if still {
		t.Error("run still tracked after its final chat_response")
	}

	got := sent()
	if len(got) != 3 {
		t.Fatalf("want all 3 chat_response chunks relayed, got %d: %+v", len(got), got)
	}
	last := got[len(got)-1]
	if last.Event.Summary != "Why don't scientists trust atoms?" {
		t.Errorf("last relayed event = %+v, want the final chunk", last.Event)
	}
}

// A turn that dies without a terminal event must not be tracked forever.
func TestChatStreamSweepsExpiredRuns(t *testing.T) {
	s, _ := newTestStream()
	s.Track("stale", "sess1")
	s.Track("fresh", "sess2")

	s.mu.Lock()
	s.runs["stale"].started = time.Now().Add(-2 * runTTL)
	s.mu.Unlock()

	s.sweep()

	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.runs["stale"]; ok {
		t.Error("expired run survived the sweep")
	}
	if _, ok := s.runs["fresh"]; !ok {
		t.Error("sweep removed a live run")
	}
}

// Delta text is read from Summary and from detail.text.
func TestDeltaText(t *testing.T) {
	cases := []struct {
		name string
		evt  domain.MonitorEvent
		want string
	}{
		{"summary", domain.MonitorEvent{Summary: "hi"}, "hi"},
		{"detail", domain.MonitorEvent{Detail: map[string]any{"text": "hi"}}, "hi"},
		{"summary wins", domain.MonitorEvent{Summary: "a", Detail: map[string]any{"text": "b"}}, "a"},
		{"neither", domain.MonitorEvent{Detail: map[string]any{"other": 1}}, ""},
	}
	for _, tc := range cases {
		if got := deltaText(tc.evt); got != tc.want {
			t.Errorf("%s: got %q, want %q", tc.name, got, tc.want)
		}
	}
}

func TestSilentFinalTerminatesMobileRequest(t *testing.T) {
	s, sent := newTestStream()
	s.Track("run1", "sess1")
	s.handle(domain.MonitorEvent{Type: "chat_response", RunID: "run1", State: "final", Summary: "NO_REPLY"})
	got := sent()
	if len(got) != 1 || got[0].Event.State != "final" || got[0].Event.Summary != "" {
		t.Fatalf("missing clean terminal event: %+v", got)
	}
	if _, exists := s.runs["run1"]; exists {
		t.Fatal("silent run still tracked")
	}
}

func TestHarnessFinalIsTerminalRegardlessOfWording(t *testing.T) {
	s, sent := newTestStream()
	s.Track("harness-run", "session")
	s.handle(domain.MonitorEvent{Type: "chat_response", RunID: "harness-run", State: "final", Summary: "Report sent to the agent.", Detail: map[string]string{"source": "harness"}})
	if got := sent(); len(got) != 1 || got[0].Event.Summary != "Report sent to the agent." {
		t.Fatalf("lost Harness text: %+v", got)
	}
	if _, tracked := s.runs["harness-run"]; tracked {
		t.Fatal("final Harness result left mobile run pending")
	}
}

func TestHarnessDeliveryReachesMQTTWithCompleteText(t *testing.T) {
	t.Chdir(t.TempDir())
	bus := monitor.ProvideBus()
	events, unsubscribe := bus.Subscribe()
	defer unsubscribe()
	h := agenthttp.ProvideAgentHandler(nil, bus, nil, &config.Config{})
	s, sent := newTestStream()
	s.Track("device-chat-mqtt-harness", "mobile-session")
	h.MarkHarnessResponseRun("device-chat-mqtt-harness", true, false)
	const text = "Kết quả Harness\n\n- Một\n- Hai"
	if !h.DeliverHarnessResponse("device-chat-mqtt-harness", text) {
		t.Fatal("delivery rejected")
	}
	for {
		select {
		case event := <-events:
			s.handle(event)
		default:
			found := false
			for _, event := range sent() {
				if event.Event.Type == "chat_response" && event.Event.State == "final" {
					if found || event.Event.Summary != text || event.Event.RunID != "device-chat-mqtt-harness" {
						t.Fatalf("wrong final: %+v", event)
					}
					found = true
				}
			}
			if !found {
				t.Fatal("MQTT did not receive Harness final")
			}
			return
		}
	}
}

// A complete reply may arrive while the sensing POST is still returning its ID.
func TestChatStreamCapturesEarlyTerminalForExactRun(t *testing.T) {
	s, sent := newTestStream()
	finish := s.capture()
	s.handle(domain.MonitorEvent{Type: "assistant_delta", RunID: "voice", Summary: "private"})
	s.handle(domain.MonitorEvent{Type: "assistant_delta", RunID: "mine", Summary: "Hello"})
	s.handle(domain.MonitorEvent{Type: "chat_response", RunID: "mine", State: "final", Summary: "Hello"})
	if len(sent()) != 0 {
		t.Fatal("unacknowledged events escaped the capture")
	}
	finish("mine", "session")
	finish("", "") // Deferred cleanup must not replay twice.
	s.flushAll()
	got := sent()
	if len(got) != 2 || got[0].Event.Type != "assistant_delta" || got[1].Event.State != "final" {
		t.Fatalf("reply not delivered in order: %+v", got)
	}
	for _, evt := range got {
		if evt.RunID != "mine" || evt.SessionID != "session" {
			t.Fatalf("unrelated event/session leaked: %+v", evt)
		}
	}
	if len(s.runs) != 0 || len(s.captures) != 0 {
		t.Fatal("terminal run or capture leaked")
	}
}

func TestChatStreamCaptureFailureAndConcurrentRequests(t *testing.T) {
	s, sent := newTestStream()
	failed := s.capture()
	first := s.capture()
	second := s.capture()
	s.handle(domain.MonitorEvent{Type: "chat_response", RunID: "first", State: "final", Summary: "one"})
	s.handle(domain.MonitorEvent{Type: "chat_response", RunID: "second", State: "final", Summary: "two"})
	failed("", "")
	second("second", "s2")
	first("first", "s1")
	got := sent()
	if len(got) != 2 || got[0].RunID != "second" || got[1].RunID != "first" {
		t.Fatalf("captures duplicated or lost events: %+v", got)
	}
	if len(s.captures) != 0 {
		t.Fatal("capture leaked")
	}
}

func TestChatStreamLiveCannotOvertakeCaptureReplay(t *testing.T) {
	s, sent := newTestStream()
	finish := s.capture()
	s.handle(domain.MonitorEvent{Type: "tool_call", RunID: "mine", Summary: "early"})
	entered := make(chan struct{})
	release := make(chan struct{})
	originalPublish := s.publish
	s.publish = func(body []byte) error {
		if string(body) != "" {
			var payload struct {
				Data domain.MQTTChatEventData `json:"data"`
			}
			if err := json.Unmarshal(body, &payload); err != nil {
				return err
			}
			if payload.Data.Event.Summary == "early" {
				close(entered)
				<-release
			}
		}
		return originalPublish(body)
	}
	replayed := make(chan struct{})
	go func() { finish("mine", "session"); close(replayed) }()
	<-entered
	liveStarted := make(chan struct{})
	liveDone := make(chan struct{})
	go func() {
		close(liveStarted)
		s.handle(domain.MonitorEvent{Type: "chat_response", RunID: "mine", State: "final", Summary: "late"})
		close(liveDone)
	}()
	<-liveStarted
	close(release)
	<-replayed
	<-liveDone
	got := sent()
	if len(got) != 2 || got[0].Event.Summary != "early" || got[1].Event.Summary != "late" {
		t.Fatalf("live event overtook replay: %+v", got)
	}
}
