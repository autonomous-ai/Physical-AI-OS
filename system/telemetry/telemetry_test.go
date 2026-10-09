package telemetry

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

// mockSender records what the pipe hands the transport.
type mockSender struct {
	mu     sync.Mutex
	events []struct {
		name   string
		params map[string]any
	}
	err  error
	done chan struct{}
}

func newMock(err error, expect int) *mockSender {
	return &mockSender{err: err, done: make(chan struct{}, expect+8)}
}

func (m *mockSender) send(_ context.Context, name string, params map[string]any) error {
	m.mu.Lock()
	m.events = append(m.events, struct {
		name   string
		params map[string]any
	}{name, params})
	m.mu.Unlock()
	m.done <- struct{}{}
	return m.err
}

func (m *mockSender) wait(t *testing.T, n int) {
	t.Helper()
	for i := 0; i < n; i++ {
		select {
		case <-m.done:
		case <-time.After(2 * time.Second):
			t.Fatalf("timed out waiting for send %d/%d", i+1, n)
		}
	}
}

// withPipe gives each test a fresh reporter and a mock transport.
func withPipe(t *testing.T, m *mockSender) {
	t.Helper()
	origGlobal := global
	global = newReporter(m.send)
	t.Cleanup(func() { global = origGlobal })
	t.Setenv("AUTONOMOUS_ANALYTICS_URL", "http://127.0.0.1:1/test")
}

func TestReportSendsWithCommonFields(t *testing.T) {
	m := newMock(nil, 1)
	withPipe(t, m)
	SetCommon(map[string]any{"os_version": "v1.2.3", "agent_runtime": "codex"})

	Report(Event{Name: "voice_metrics_interaction", ID: "e1", Params: map[string]any{"latency_ms": 1200}})
	m.wait(t, 1)

	got := m.events[0]
	if got.name != "voice_metrics_interaction" {
		t.Errorf("event name = %q", got.name)
	}
	for k, want := range map[string]any{
		"os_version":    "v1.2.3",
		"agent_runtime": "codex",
		"latency_ms":    1200,
		"event_id":      "e1",
	} {
		if got.params[k] != want {
			t.Errorf("params[%q] = %v, want %v", k, got.params[k], want)
		}
	}
}

// A producer that retries after a failed HTTP post must not inflate counts.
func TestDuplicateEventIDSentOnce(t *testing.T) {
	m := newMock(nil, 1)
	withPipe(t, m)

	Report(Event{Name: "voice_metrics_interaction", ID: "same"})
	Report(Event{Name: "voice_metrics_interaction", ID: "same"})
	m.wait(t, 1)

	time.Sleep(50 * time.Millisecond)
	m.mu.Lock()
	defer m.mu.Unlock()
	if len(m.events) != 1 {
		t.Fatalf("sent %d events, want 1", len(m.events))
	}
	if _, _, deduped := Stats(); deduped != 1 {
		t.Errorf("deduped = %d, want 1", deduped)
	}
}

// An empty ID cannot be de-duplicated; two such events are two observations.
func TestEmptyIDIsNotDeduped(t *testing.T) {
	m := newMock(nil, 2)
	withPipe(t, m)

	Report(Event{Name: "voice_metrics_interaction"})
	Report(Event{Name: "voice_metrics_interaction"})
	m.wait(t, 2)
}

// Delivery failures must be counted, never swallowed.
func TestDeliveryFailureIsCounted(t *testing.T) {
	m := newMock(errors.New("network down"), 2)
	withPipe(t, m)

	// The mock's notification precedes the worker's counter update. Exercise
	// delivery synchronously so the assertions wait for all delivery bookkeeping;
	// the other Report tests cover asynchronous queueing and transport calls.
	global.send(context.Background(), Event{Name: "voice_metrics_interaction", ID: "f1"})
	if _, failed, _ := Stats(); failed != 1 {
		t.Fatalf("failed = %d, want 1", failed)
	}

	global.send(context.Background(), Event{Name: "voice_metrics_interaction", ID: "f2"})
	if got := m.events[1].params["telemetry_failed_total"]; got != int64(1) {
		t.Errorf("telemetry_failed_total = %v, want 1", got)
	}
}

// A stalled transport must never block the caller (the voice path).
func TestFullQueueDropsInsteadOfBlocking(t *testing.T) {
	m := newMock(nil, 0)
	withPipe(t, m)
	// Block the worker so the queue actually fills.
	release := make(chan struct{})
	global = newReporter(func(ctx context.Context, name string, params map[string]any) error {
		<-release
		return nil
	})
	t.Cleanup(func() { close(release) })

	done := make(chan struct{})
	go func() {
		for i := 0; i < queueSize+5; i++ {
			Report(Event{Name: "voice_metrics_interaction"})
		}
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Report blocked on a full queue")
	}
	if dropped, _, _ := Stats(); dropped == 0 {
		t.Error("a full queue must drop and count, not block")
	}
}

func TestUnnamedEventIsIgnored(t *testing.T) {
	m := newMock(nil, 0)
	withPipe(t, m)
	Report(Event{ID: "no-name"})
	time.Sleep(50 * time.Millisecond)
	m.mu.Lock()
	defer m.mu.Unlock()
	if len(m.events) != 0 {
		t.Errorf("sent %d events for a nameless report", len(m.events))
	}
}

// No endpoint configured: a body nobody set up must keep its events on-device.
func TestSendingIsOffWithoutAnEndpoint(t *testing.T) {
	m := newMock(nil, 0)
	withPipe(t, m)
	t.Setenv("AUTONOMOUS_ANALYTICS_URL", "")

	Report(Event{Name: "voice_metrics_interaction", ID: "off-1"})
	time.Sleep(50 * time.Millisecond)

	m.mu.Lock()
	defer m.mu.Unlock()
	if len(m.events) != 0 {
		t.Fatalf("sent %d events with no endpoint configured", len(m.events))
	}
}

func TestEnabledFollowsTheConfiguredEndpoint(t *testing.T) {
	t.Setenv("AUTONOMOUS_ANALYTICS_URL", "https://example.test/api")
	if !Enabled() {
		t.Error("Enabled() = false with an endpoint configured")
	}
	t.Setenv("AUTONOMOUS_ANALYTICS_URL", "")
	if Enabled() {
		t.Error("Enabled() = true with no endpoint configured")
	}
}
