package harness

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func storeFixtureRequests(t *testing.T) []Frame {
	t.Helper()
	raw, err := os.ReadFile("../../docs/contracts/autonomous-device-store-v1/blender.fixture.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		Steps []struct {
			Request Frame `json:"request"`
		} `json:"steps"`
	}
	if err = json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	var frames []Frame
	for _, step := range fixture.Steps {
		if isStoreOperation(stringField(step.Request, "type")) {
			frames = append(frames, step.Request)
		}
	}
	if len(frames) < 4 {
		t.Fatal("missing contract operations")
	}
	return frames
}

func storeTestService() *Service {
	caps := map[string]bool{}
	for _, cap := range capabilities {
		caps[cap] = true
	}
	return &Service{disk: diskState{Peer: &peer{MachineID: "mac-example"}}, conn: &connection{channel: &DirectChannel{}, capabilities: caps, pending: map[string]chan response{}}}
}

func TestStoreContractFixtureEncryptedRequests(t *testing.T) {
	key := []byte(strings.Repeat("s", 32))
	received := make(chan Frame, 1)
	upgrader := websocket.Upgrader{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ws, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer ws.Close()
		for {
			var frame Frame
			if ws.ReadJSON(&frame) != nil {
				return
			}
			received <- frame
		}
	}))
	defer server.Close()
	ws, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer ws.Close()
	s := storeTestService()
	s.conn.channel = &DirectChannel{ws: ws, crypto: &deviceSessionCrypto{c2s: key}}
	for index, frame := range storeFixtureRequests(t) {
		if err := validateStoreRequest(frame); err != nil {
			t.Fatal(err)
		}
		done := make(chan error, 1)
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		go func() { _, err := s.Request(ctx, frame); done <- err }()
		select {
		case wire := <-received:
			if stringField(wire, "type") != "autonomous_device_request" {
				t.Fatal("wrong envelope")
			}
			env := payloadOf(wire)["__e2e"].(map[string]any)
			cipher, err := decode(env["ct"], -1)
			if err != nil {
				t.Fatal(err)
			}
			raw, err := openSealed(key, uint64(index), []byte("1|autonomous_device_request||p|"), cipher)
			if err != nil {
				t.Fatal(err)
			}
			var actual Frame
			if err = json.Unmarshal(raw, &actual); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(actual, frame) {
				t.Fatalf("changed owner fixture: %v", actual)
			}
			s.conn.mu.Lock()
			pending := s.conn.pending[stringField(frame, "requestId")]
			s.conn.mu.Unlock()
			pending <- response{frame: Frame{"type": stringField(frame, "type") + "_result", "requestId": frame["requestId"]}}
		case <-ctx.Done():
			t.Fatal("encrypted request was not sent")
		}
		if err := <-done; err != nil {
			t.Fatal(err)
		}
		cancel()
	}
}

func TestStoreCapabilityAndMachineGates(t *testing.T) {
	for _, missing := range storeCapabilities {
		s := storeTestService()
		delete(s.conn.capabilities, missing)
		for _, frame := range storeFixtureRequests(t) {
			_, err := s.Request(context.Background(), frame)
			if err == nil || !strings.Contains(err.Error(), "UNSUPPORTED_CAPABILITY") {
				t.Fatalf("missing %s: %v", missing, err)
			}
		}
	}
	s := storeTestService()
	_, err := s.Request(context.Background(), Frame{"type": "agent.prepare", "machineId": "other"})
	if err == nil || err.Error() != "MACHINE_MISMATCH" {
		t.Fatal(err)
	}
}

func TestStoreStrictValidation(t *testing.T) {
	for _, id := range []any{nil, 123, "", "not-uuid"} {
		_, err := storeTestService().Request(context.Background(), Frame{"type": "store.list", "requestId": id})
		if err == nil || !strings.Contains(err.Error(), "INVALID_REQUEST") {
			t.Fatalf("accepted supplied invalid requestId %v: %v", id, err)
		}
	}
	valid := Frame{"type": "agent.prepare", "requestId": "33333333-3333-4333-8333-333333333333", "machineId": "mac-example", "packageId": "autonomous/blender", "idempotencyKey": "prepare-airplane-001", "workspace": Frame{"kind": "new"}}
	if err := validateStoreRequest(valid); err != nil {
		t.Fatal(err)
	}
	for _, change := range []Frame{
		{"text": "Do work"}, {"agentId": "existing-agent"}, {"prompt": "Do work"}, {"idempotencyKey": "bad/key"}, {"packageId": "https://example.com/package"}, {"requestId": "not-uuid"},
		{"workspace": Frame{"kind": "new", "name": "../escape"}}, {"workspace": Frame{"kind": "new", "path": "/tmp"}}, {"workspace": Frame{"kind": "existing", "path": "relative"}}, {"workspace": Frame{"kind": "existing", "path": "/tmp\nunsafe"}},
	} {
		f := Frame{}
		for k, v := range valid {
			f[k] = v
		}
		for k, v := range change {
			f[k] = v
		}
		if err := validateStoreRequest(f); err == nil {
			t.Fatalf("accepted %v", change)
		}
	}
	for _, change := range []Frame{{"query": strings.Repeat("q", 201)}, {"limit": 11}, {"offset": -1}, {"limit": 1.5}, {"machineId": "mac-example"}, {"query": nil}} {
		f := Frame{"type": "store.list", "requestId": valid["requestId"]}
		for k, v := range change {
			f[k] = v
		}
		if err := validateStoreRequest(f); err == nil {
			t.Fatalf("accepted %v", change)
		}
	}
}

func TestStorePreparationUncertaintyIsNotTaskDelivery(t *testing.T) {
	s := storeTestService()
	var prepare Frame
	for _, frame := range storeFixtureRequests(t) {
		if frame["type"] == "agent.prepare" {
			prepare = frame
			break
		}
	}
	_, err := s.Request(context.Background(), prepare)
	var uncertain *PreparationUnknownError
	var delivery *DeliveryUnknownError
	if !errors.As(err, &uncertain) || errors.As(err, &delivery) || uncertain.RequestID != prepare["requestId"] {
		t.Fatalf("wrong preparation recovery: %v", err)
	}
	_, err = s.Request(context.Background(), Frame{"type": "turn.send", "machineId": "mac-example", "agentId": "agent-example", "idempotencyKey": "task-airplane-001", "text": "draw airplane"})
	if !errors.As(err, &delivery) || errors.As(err, &uncertain) {
		t.Fatalf("wrong task recovery: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err = s.Request(ctx, prepare)
	if !errors.Is(err, context.Canceled) || errors.As(err, &uncertain) {
		t.Fatalf("pre-send cancellation is not uncertain: %v", err)
	}
}

func TestStoreTimeoutAfterEncryptedSendPreservesRecoveryClass(t *testing.T) {
	for _, kind := range []string{"agent.prepare", "turn.send", "operation.get"} {
		t.Run(kind, func(t *testing.T) {
			received := make(chan struct{}, 1)
			release := make(chan struct{})
			upgrader := websocket.Upgrader{}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				ws, err := upgrader.Upgrade(w, r, nil)
				if err != nil {
					return
				}
				defer ws.Close()
				var frame Frame
				if ws.ReadJSON(&frame) == nil {
					received <- struct{}{}
				}
				<-release
			}))
			defer server.Close()
			defer close(release)
			ws, _, err := websocket.DefaultDialer.Dial("ws"+strings.TrimPrefix(server.URL, "http"), nil)
			if err != nil {
				t.Fatal(err)
			}
			defer ws.Close()
			s := storeTestService()
			s.conn.channel = &DirectChannel{ws: ws, crypto: &deviceSessionCrypto{c2s: []byte(strings.Repeat("s", 32))}}
			frame := Frame{"type": "turn.send", "agentId": "agent-example", "machineId": "mac-example", "text": "work", "idempotencyKey": "task-key"}
			if kind != "turn.send" {
				for _, f := range storeFixtureRequests(t) {
					if f["type"] == kind {
						frame = f
						break
					}
				}
			}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			done := make(chan error, 1)
			go func() { _, err := s.Request(ctx, frame); done <- err }()
			select {
			case <-received:
			case <-time.After(time.Second):
				t.Fatal("request not sent")
			}
			cancel()
			err = <-done
			var preparation *PreparationUnknownError
			var delivery *DeliveryUnknownError
			if !errors.Is(err, context.Canceled) {
				t.Fatal(err)
			}
			if errors.As(err, &preparation) != (kind == "agent.prepare") || errors.As(err, &delivery) != (kind == "turn.send") {
				t.Fatalf("incorrect recovery for %s: %v", kind, err)
			}
			if len(s.conn.pending) != 0 {
				t.Fatal("pending request leaked")
			}
		})
	}
}
