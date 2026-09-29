package codex

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	"go.autonomous.ai/os/system/lib/sensingmsg"
	"go.autonomous.ai/os/system/lib/speakergate"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
)

func reconnectReplayService(t *testing.T) *CodexService {
	t.Helper()
	sensingmsg.SetHarnessConnected(func() bool { return true })
	t.Cleanup(func() { sensingmsg.SetHarnessConnected(nil) })
	oldProbe := speakergate.SpeakerBusy
	speakergate.SpeakerBusy = func() bool { return false }
	t.Cleanup(func() { speakergate.SpeakerBusy = oldProbe })
	return &CodexService{config: &config.Config{}, monitorBus: monitor.ProvideBus()}
}

func TestConnectionReadyDrainsOnlyUnsentEventsOnceWithoutTurnEnd(t *testing.T) {
	s := reconnectReplayService(t)
	s.addPendingRun("already-sent", "old-run")
	s.pendingEvents = []pendingEvent{{eventType: "web_chat", msg: "Please answer in English.", fixedRunID: "queued-user", queuedAt: time.Now()}}
	s.drainPendingEvents()
	if len(s.pendingEvents) != 1 {
		t.Fatal("offline drain discarded unsent event")
	}

	received := make(chan map[string]any, 1)
	peerDone := make(chan error, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		peer, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
		if err != nil {
			peerDone <- err
			return
		}
		defer peer.Close()
		for i := 0; i < 2; i++ {
			if err := peer.WriteJSON(map[string]any{"type": "bridge.status", "state": "ready"}); err != nil {
				peerDone <- err
				return
			}
		}
		_ = peer.SetReadDeadline(time.Now().Add(3 * time.Second))
		var frame map[string]any
		if err := peer.ReadJSON(&frame); err != nil {
			peerDone <- fmt.Errorf("queued request did not drain without a terminal event: %w", err)
			return
		}
		received <- frame
		_ = peer.SetReadDeadline(time.Now().Add(250 * time.Millisecond))
		_, _, err = peer.ReadMessage()
		if netErr, ok := err.(net.Error); !ok || !netErr.Timeout() {
			peerDone <- fmt.Errorf("unexpected extra transmission or peer error: %v", err)
			return
		}
		peerDone <- nil
	}))
	defer server.Close()
	oldURL := WSURL
	WSURL = "ws" + strings.TrimPrefix(server.URL, "http")
	defer func() { WSURL = oldURL }()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	connectionDone := make(chan error, 1)
	go func() { connectionDone <- s.runWSConn(ctx, nil) }()
	select {
	case frame := <-received:
		if frame["type"] != "message.send" || frame["id"] != "queued-user" || frame["run_id"] != "queued-user" {
			t.Errorf("wrong request replayed: %v", frame)
		}
		payload, _ := frame["payload"].(map[string]any)
		if !strings.Contains(fmt.Sprint(payload["content"]), "Please answer in English.") || !strings.Contains(fmt.Sprint(payload["content"]), "[harness-reply run_id=queued-user channel=web]") {
			t.Error("queued user content changed")
		}
		if dispatch, _ := s.wsDispatch.Load().(dispatchFn); dispatch == nil {
			t.Error("replay began before event dispatch was initialized")
		}
		// Ready-time replay must not consume or resend the old correlation record.
		s.pendingMu.Lock()
		if len(s.pendingRuns) != 2 || s.pendingRuns[0].reqID != "already-sent" {
			t.Errorf("ready-time drain changed transmitted request records: %+v", s.pendingRuns)
		}
		s.pendingMu.Unlock()
		// Concurrent idle/ready callbacks must not duplicate the detached queue.
		var drains sync.WaitGroup
		for i := 0; i < 8; i++ {
			drains.Add(1)
			go func() { defer drains.Done(); s.DrainPendingEvents() }()
		}
		drains.Wait()
	case <-time.After(4 * time.Second):
		t.Fatal("no connection-ready replay")
	}
	if err := <-peerDone; err != nil {
		t.Error(err)
	}
	select {
	case <-connectionDone:
	case <-time.After(time.Second):
		t.Fatal("connection did not stop after peer closed")
	}
	s.pendingEventsMu.Lock()
	remaining := len(s.pendingEvents)
	s.pendingEventsMu.Unlock()
	if remaining != 0 {
		t.Fatal("unsent event remained queued after successful write")
	}

}

func TestDrainRetainsEventWhenConnectionVanishesBeforeAnyWrite(t *testing.T) {
	s := reconnectReplayService(t)
	// Socket cleared before wsConnected's teardown flag: sendFrame must identify
	// this as definitely unsent, distinct from an uncertain WriteMessage error.
	s.wsConnected.Store(true)
	s.pendingEvents = []pendingEvent{{eventType: "web_chat", msg: "retain me", fixedRunID: "unsent", queuedAt: time.Now()}}
	s.drainPendingEvents()
	if len(s.pendingEvents) != 1 || s.pendingEvents[0].fixedRunID != "unsent" {
		t.Fatal("definitely unsent event was lost")
	}
	if s.hasPendingRuns() {
		t.Fatal("unsent request left a transmitted-run correlation entry")
	}
}

func TestDisconnectedBeforeSendIsNotAWriteFailure(t *testing.T) {
	s := &CodexService{}
	if err := s.sendFrame(json.RawMessage(`{"type":"ping"}`)); err != errDisconnectedBeforeSend {
		t.Fatalf("disconnected send did not preserve definitely-unsent classification: %v", err)
	}
}
