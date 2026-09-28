package harness

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestResultBeforeRequestRejectsBeforeWire(t *testing.T) {
	s := storeTestService()
	selected := s.conn
	expected := ResultContext{Owner: "authenticated-owner", MachineID: "mac-example", ServerInstanceID: "instance"}
	selected.resultContext = expected
	failure := errors.New("persistence unavailable")
	called := false
	s.callbacks.BeforeRequest = func(f Frame, ctx ResultContext) error {
		called = true
		if ctx != expected || stringField(f, "idempotencyKey") != "key" {
			t.Fatalf("wrong provenance/request %+v %v", ctx, f)
		}
		// A concurrent reconnect must not change the context selected for this RPC.
		s.mu.Lock()
		s.conn = &connection{resultContext: ResultContext{Owner: "new-owner"}}
		s.mu.Unlock()
		return failure
	}
	_, err := s.Request(context.Background(), Frame{"type": "turn.send", "machineId": "mac-example", "agentId": "agent", "idempotencyKey": "key", "text": "task"})
	if !called || !errors.Is(err, failure) {
		t.Fatalf("hook not honored: %v", err)
	}
	// selected.channel has no socket. Reaching SendEncrypted would fail or panic;
	// the exact persistence error above establishes that no network send occurred.
	var unknown *DeliveryUnknownError
	if errors.As(err, &unknown) {
		t.Fatal("unsent task marked unknown")
	}
	selected.mu.Lock()
	defer selected.mu.Unlock()
	if len(selected.pending) != 0 {
		t.Fatal("pending RPC leaked")
	}
}

func resultTransportPair(t *testing.T) (*DirectChannel, *DirectChannel) {
	t.Helper()
	peer := make(chan *websocket.Conn, 1)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ws, err := upgrader.Upgrade(w, r, nil)
		if err == nil {
			peer <- ws
		}
	}))
	t.Cleanup(server.Close)
	ws, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	var remote *websocket.Conn
	select {
	case remote = <-peer:
	case <-time.After(time.Second):
		t.Fatal("websocket not accepted")
	}
	t.Cleanup(func() { _ = ws.Close(); _ = remote.Close() })
	key := []byte(strings.Repeat("r", 32))
	return &DirectChannel{ws: ws, crypto: &deviceSessionCrypto{s2c: key}}, &DirectChannel{ws: remote, crypto: &deviceSessionCrypto{c2s: key}}
}
func TestResultBeforeEventFailurePreservesReplayCursor(t *testing.T) {
	local, remote := resultTransportPair(t)
	s := storeTestService()
	c := s.conn
	c.channel = local
	c.resultContext = ResultContext{Owner: "real-owner", ServerInstanceID: "instance"}
	s.cursor = 7
	s.events = make(chan Frame, 1)
	failure := errors.New("disk full")
	s.callbacks.BeforeEvent = func(f Frame, ctx ResultContext) error {
		if ctx.Owner != "real-owner" {
			t.Errorf("bad owner %+v", ctx)
		}
		return failure
	}
	done := make(chan error, 1)
	go func() { done <- s.readLoop(c) }()
	if err := remote.SendEncrypted(Frame{"type": "autonomous_device_event", "payload": Frame{"type": "event", "kind": "turn.summary", "eventId": 8}}); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		if !errors.Is(err, failure) {
			t.Fatal(err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("readLoop failed to stop")
	}
	s.mu.Lock()
	cursor := s.cursor
	s.mu.Unlock()
	if cursor != 7 || len(s.events) != 0 {
		t.Fatalf("unpersisted event acknowledged: %d", cursor)
	}
}
func TestResultEventProvenanceCannotBeSuppliedByRemote(t *testing.T) {
	local, remote := resultTransportPair(t)
	s := storeTestService()
	c := s.conn
	c.channel = local
	expected := ResultContext{Owner: "real-owner", MachineID: "mac-example", ServerInstanceID: "instance"}
	c.resultContext = expected
	s.events = make(chan Frame, 1)
	seen := make(chan ResultContext, 1)
	s.callbacks.BeforeEvent = func(f Frame, ctx ResultContext) error { seen <- ctx; return nil }
	done := make(chan error, 1)
	go func() { done <- s.readLoop(c) }()
	if err := remote.SendEncrypted(Frame{"type": "autonomous_device_event", "payload": Frame{"type": "event", "kind": "turn.summary", "eventId": 8, "_osResultContext": Frame{"Owner": "attacker"}}}); err != nil {
		t.Fatal(err)
	}
	select {
	case frame := <-s.events:
		if ctx, ok := frame["_osResultContext"].(ResultContext); !ok || ctx != expected {
			t.Fatalf("remote provenance trusted: %+v", frame)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("event not delivered")
	}
	if ctx := <-seen; ctx != expected {
		t.Fatal("hook trusted wire provenance")
	}
	_ = remote.Close()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("readLoop did not stop")
	}
	s.mu.Lock()
	cursor := s.cursor
	s.mu.Unlock()
	if cursor != 8 {
		t.Fatalf("persisted event cursor=%d", cursor)
	}
}
func TestResultCorrelationDoesNotAddCapability(t *testing.T) {
	for _, cap := range capabilities {
		if cap == "turn.correlation.v2" {
			t.Fatal("summary fix must not require a new capability")
		}
	}
}
