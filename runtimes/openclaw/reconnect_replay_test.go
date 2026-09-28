package openclaw

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
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

func reconnectService(t *testing.T) *OpenclawService {
	t.Helper()
	sensingmsg.SetHarnessConnected(func() bool { return true })
	t.Cleanup(func() { sensingmsg.SetHarnessConnected(nil) })
	old := speakergate.SpeakerBusy
	speakergate.SpeakerBusy = func() bool { return false }
	t.Cleanup(func() { speakergate.SpeakerBusy = old })
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "openclaw.json"), []byte(`{"gateway":{"auth":{"token":"test-token"}}}`), 0600); err != nil {
		t.Fatal(err)
	}
	return ProvideService(&config.Config{OpenclawConfigDir: dir}, monitor.ProvideBus(), nil)
}

func TestReconnectDrainsUnsentAfterAuthenticatedHandshake(t *testing.T) {
	s := reconnectService(t)
	s.SetPendingChatTrace("already-sent", "never replay this")
	s.pendingEvents = []pendingEvent{{eventType: "web_chat", msg: "Open Notes", fixedRunID: "queued-user", queuedAt: time.Now()}}
	s.DrainPendingEvents()
	if len(s.pendingEvents) != 1 {
		t.Fatal("offline callback lost user request")
	}
	got := make(chan map[string]any, 1)
	done := make(chan error, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		peer, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
		if err != nil {
			done <- err
			return
		}
		defer peer.Close()
		_ = peer.SetReadDeadline(time.Now().Add(3 * time.Second))
		if err = peer.WriteJSON(map[string]any{"payload": map[string]any{"nonce": "test-nonce"}}); err != nil {
			done <- err
			return
		}
		var frame map[string]any
		if err = peer.ReadJSON(&frame); err != nil {
			done <- err
			return
		}
		if frame["method"] != "connect" {
			done <- errors.New("chat sent before authentication")
			return
		}
		if err = peer.WriteJSON(map[string]any{"type": "res", "ok": true, "result": map[string]any{"sessionKey": "main"}}); err != nil {
			done <- err
			return
		}
		if err = peer.ReadJSON(&frame); err != nil {
			done <- err
			return
		}
		if frame["method"] != "sessions.subscribe" {
			done <- errors.New("chat sent before subscription")
			return
		}
		frame = nil
		if err = peer.ReadJSON(&frame); err != nil {
			done <- err
			return
		}
		got <- frame
		_ = peer.SetReadDeadline(time.Now().Add(200 * time.Millisecond))
		_, _, err = peer.ReadMessage()
		if ne, ok := err.(net.Error); !ok || !ne.Timeout() {
			done <- errors.New("duplicate or uncertain request replayed")
			return
		}
		done <- nil
	}))
	defer server.Close()
	connDone := make(chan error, 1)
	go func() {
		connDone <- s.runWSConnAt(context.Background(), nil, "ws"+strings.TrimPrefix(server.URL, "http"))
	}()
	select {
	case frame := <-got:
		params, _ := frame["params"].(map[string]any)
		if frame["method"] != "chat.send" || params["idempotencyKey"] != "queued-user" || params["message"] != "[user] Open Notes\n[harness-reply run_id=queued-user channel=web]"+"\n"+sensingmsg.HarnessConnectedContext {
			t.Errorf("wrong replay: %v", frame)
		}
		var wg sync.WaitGroup
		for i := 0; i < 8; i++ {
			wg.Add(1)
			go func() { defer wg.Done(); s.DrainPendingEvents() }()
		}
		wg.Wait()
	case <-time.After(4 * time.Second):
		t.Fatal("no drain after handshake")
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	<-connDone
	if !s.RemovePendingChatTraceByRunID("already-sent") {
		t.Fatal("transmitted trace was consumed as replay")
	}
}

func TestDrainRetainsDefinitelyUnsentOnMissingSocket(t *testing.T) {
	s := reconnectService(t)
	s.wsConnected.Store(true)
	s.pendingEvents = []pendingEvent{{eventType: "web_chat", msg: "keep", fixedRunID: "unsent", queuedAt: time.Now()}}
	s.DrainPendingEvents()
	if len(s.pendingEvents) != 1 || s.pendingEvents[0].fixedRunID != "unsent" {
		t.Fatal("unsent request lost")
	}
}

func TestDrainDoesNotReplayUncertainWrite(t *testing.T) {
	s := reconnectService(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer p.Close()
		_, _, _ = p.ReadMessage()
	}))
	defer server.Close()
	conn, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	conn.Close()
	s.wsConn = conn
	s.wsConnected.Store(true)
	s.pendingEvents = []pendingEvent{{eventType: "web_chat", msg: "uncertain", fixedRunID: "uncertain", queuedAt: time.Now()}}
	s.DrainPendingEvents()
	if len(s.pendingEvents) != 0 {
		t.Fatal("attempted write must not be automatically replayed")
	}
}

func TestRejectedHandshakeDoesNotDrain(t *testing.T) {
	s := reconnectService(t)
	s.pendingEvents = []pendingEvent{{eventType: "web_chat", msg: "keep", queuedAt: time.Now()}}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer p.Close()
		_ = p.WriteJSON(map[string]any{"payload": map[string]any{"nonce": "test"}})
		var req json.RawMessage
		_ = p.ReadJSON(&req)
		_ = p.WriteJSON(map[string]any{"type": "res", "ok": false, "error": map[string]any{"code": "AUTH_FAILED"}})
	}))
	defer server.Close()
	if err := s.runWSConnAt(context.Background(), nil, "ws"+strings.TrimPrefix(server.URL, "http")); err == nil {
		t.Fatal("rejected authentication treated as ready")
	}
	if len(s.pendingEvents) != 1 || s.wsConnected.Load() {
		t.Fatal("rejected connection drained queue")
	}
}
