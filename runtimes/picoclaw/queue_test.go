package picoclaw

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/sensingmsg"
	"go.autonomous.ai/os/system/lib/speakergate"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
)

func queueService(t *testing.T) *PicoclawService {
	t.Helper()
	sensingmsg.SetHarnessConnected(func() bool { return true })
	t.Cleanup(func() { sensingmsg.SetHarnessConnected(nil) })
	old := speakergate.SpeakerBusy
	speakergate.SpeakerBusy = func() bool { return false }
	t.Cleanup(func() { speakergate.SpeakerBusy = old })
	return ProvideService(&config.Config{}, monitor.ProvideBus(), nil)
}

func queueSocket(t *testing.T, s *PicoclawService) <-chan map[string]any {
	t.Helper()
	frames := make(chan map[string]any, 10)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer c.Close()
		for {
			var f map[string]any
			if c.ReadJSON(&f) != nil {
				return
			}
			frames <- f
		}
	}))
	c, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	s.wsConn = c
	s.wsConnected.Store(true)
	t.Cleanup(func() { c.Close(); server.Close() })
	return frames
}

func nextQueuedFrame(t *testing.T, frames <-chan map[string]any, id string) map[string]any {
	t.Helper()
	select {
	case f := <-frames:
		if f["id"] != id {
			t.Fatalf("wanted %s, got %v", id, f)
		}
		return f
	case <-time.After(2 * time.Second):
		t.Fatalf("missing request %s", id)
		return nil
	}
}

func TestQueueSerializesDirectAndBufferedTurnsThroughAllTerminalCallbacks(t *testing.T) {
	s := queueService(t)
	frames := queueSocket(t, s)
	if _, err := s.SendChatMessageWithRun("first", "req-1", "run-1"); err != nil {
		t.Fatal(err)
	}
	nextQueuedFrame(t, frames, "req-1")
	if _, err := s.SendChatMessageWithImagesAndRun("second", []string{"image-data"}, "req-2", "run-2"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.SendChatMessageWithRun("third", "req-3", "run-3"); err != nil {
		t.Fatal(err)
	}
	s.DrainPendingEvents()
	if s.peekPendingRunID() != "run-1" {
		t.Fatal("queued request overwrote first correlation")
	}
	callback := func(e domain.WSEvent) {
		var p struct {
			RunID string `json:"runId"`
		}
		_ = json.Unmarshal(e.Payload, &p)
		if p.RunID != "run-1" {
			t.Errorf("wrong first terminal correlation: %s", p.RunID)
		}
		s.SetBusy(false)
		if s.peekPendingRunID() == "run-2" {
			t.Error("next turn sent before all terminal callbacks")
		}
	}
	s.translateFrame([]byte(`{"type":"message.create","payload":{"content":"answer one"}}`), callback)
	f := nextQueuedFrame(t, frames, "req-2")
	payload := f["payload"].(map[string]any)
	if payload["content"] != "second\n[harness-reply run_id=run-2 channel=web]"+"\n"+sensingmsg.HarnessConnectedContext || len(payload["attachments"].([]any)) != 1 {
		t.Fatalf("queued chat changed: %v", f)
	}
	s.SetBusy(false) // A repeated idle from the old consumer cannot drain turn 3.
	if s.peekPendingRunID() != "run-2" {
		t.Fatal("duplicate idle cleared next turn")
	}
	s.translateFrame([]byte(`{"type":"error","payload":{"message":"turn two failed"}}`), func(e domain.WSEvent) { s.SetBusy(false) })
	nextQueuedFrame(t, frames, "req-3")
	if s.peekPendingRunID() != "run-3" {
		t.Fatal("error did not advance exactly one turn")
	}
	select {
	case f := <-frames:
		t.Fatalf("unexpected transmission: %v", f)
	case <-time.After(30 * time.Millisecond):
	}
}

func TestReconnectDrainsOnlyUnsentWithoutTerminalEvent(t *testing.T) {
	observations := captureLostObservations(t)
	s := queueService(t)
	s.pendingEvents = []pendingEvent{{eventType: "web_chat", msg: "retained", fixedRunID: "unsent", queuedAt: time.Now()}}
	s.drainPendingEvents()
	if len(s.pendingEvents) != 1 {
		t.Fatal("offline callback dropped unsent request")
	}
	received := make(chan map[string]any, 2)
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer c.Close()
		var f map[string]any
		if c.ReadJSON(&f) == nil {
			received <- f
		}
		<-release
	}))
	defer server.Close()
	done := make(chan error, 1)
	go func() { done <- s.runWSConnURL(context.Background(), nil, "ws"+strings.TrimPrefix(server.URL, "http")) }()
	nextQueuedFrame(t, received, "unsent")
	close(release)
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("connection teardown did not finish")
	}
	assertLostObservation(t, observations, "unsent")
	// The just-written request is uncertain on disconnect, not locally queued.
	if len(s.pendingEvents) != 0 || s.peekPendingRunID() != "" || s.activeTurn.Load() {
		t.Fatal("disconnect retained transmitted work")
	}
}

func TestMissingSocketRetainsUnsentButFailedWriteDoesNotReplay(t *testing.T) {
	s := queueService(t)
	s.wsConnected.Store(true)
	s.pendingEvents = []pendingEvent{{eventType: "web_chat", msg: "first", fixedRunID: "first"}, {eventType: "web_chat", msg: "second", fixedRunID: "second"}}
	s.drainPendingEvents()
	if len(s.pendingEvents) != 2 || s.peekPendingRunID() != "" {
		t.Fatal("missing socket lost unsent request")
	}
	queueSocket(t, s)
	_ = s.wsConn.Close() // A write attempt on this socket returns an uncertain write error.
	s.drainPendingEvents()
	if len(s.pendingEvents) != 1 || s.pendingEvents[0].fixedRunID != "second" {
		t.Fatalf("wrong retry queue after write failure: %+v", s.pendingEvents)
	}
	if s.wsConnected.Load() {
		t.Fatal("failed write did not close admission until reconnect")
	}
	s.drainPendingEvents()
	if len(s.pendingEvents) != 1 {
		t.Fatal("offline drain lost untouched tail")
	}
}

func TestExpiredTurnRetiresUncorrelatedTransportBeforeQueueDrain(t *testing.T) {
	s := queueService(t)
	frames := queueSocket(t, s)
	if _, err := s.SendChatMessageWithRun("old", "old", "old"); err != nil {
		t.Fatal(err)
	}
	nextQueuedFrame(t, frames, "old")
	s.QueuePendingEvent("web_chat", "next", nil, "next")
	s.busySince.Store(time.Now().Add(-busyTTL - time.Second).UnixMilli())
	if s.IsBusy() {
		t.Fatal("expired turn remained busy")
	}
	if s.IsReady() {
		t.Fatal("expired uncorrelated transport still admits work")
	}
	s.DrainPendingEvents()
	if len(s.pendingEvents) != 1 {
		t.Fatal("unsent next turn lost while retiring old connection")
	}
}
