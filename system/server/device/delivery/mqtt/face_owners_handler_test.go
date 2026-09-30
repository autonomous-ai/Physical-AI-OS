package mqtthandler

import (
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// faceStub fakes HAL and the backend privacy endpoint behind http.DefaultTransport.
type faceStub struct {
	mu      sync.Mutex
	replies map[string]faceStubReply // by URL path
	calls   chan faceStubCall
}

type faceStubReply struct {
	status int
	body   string
}

type faceStubCall struct {
	path string
	body map[string]any
}

func (s *faceStub) set(path string, status int, body string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.replies[path] = faceStubReply{status, body}
}

func (s *faceStub) RoundTrip(r *http.Request) (*http.Response, error) {
	var body map[string]any
	if r.Body != nil {
		_ = json.NewDecoder(r.Body).Decode(&body)
	}
	s.mu.Lock()
	rep, ok := s.replies[r.URL.Path]
	s.mu.Unlock()
	if !ok {
		rep = faceStubReply{http.StatusNotFound, `{"detail":"no stub"}`}
	}
	s.calls <- faceStubCall{r.URL.Path, body}
	return &http.Response{StatusCode: rep.status, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(rep.body))}, nil
}

func (s *faceStub) noCall(t *testing.T) {
	t.Helper()
	select {
	case c := <-s.calls:
		t.Fatalf("unexpected call %s %v", c.path, c.body)
	default:
	}
}

func newFaceTest(t *testing.T) (*DeviceMQTTHandler, *faceStub, func(t *testing.T, kind, want string) domain.MQTTDataResponse) {
	stub := &faceStub{replies: map[string]faceStubReply{}, calls: make(chan faceStubCall, 8)}
	original := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = original })
	http.DefaultTransport = stub
	factory, messages := statusBroker(t)
	h := &DeviceMQTTHandler{config: &config.Config{
		DeviceID: "face-test", FDChannel: "test/fd", LLMBaseURL: "http://backend.test/v1", LLMAPIKey: "k",
	}, mqttFactory: factory}
	read := func(t *testing.T, kind, want string) domain.MQTTDataResponse {
		t.Helper()
		select {
		case payload := <-messages:
			var reply domain.MQTTDataResponse
			if err := json.Unmarshal(payload, &reply); err != nil {
				t.Fatal(err)
			}
			if reply.Kind != kind || reply.Status != want {
				t.Fatalf("reply = %s; want %s %q", payload, kind, want)
			}
			return reply
		case <-time.After(3 * time.Second):
			t.Fatalf("missing MQTT %s %s reply", kind, want)
		}
		return domain.MQTTDataResponse{}
	}
	return h, stub, read
}

func dataJSON(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestFaceOwnersListsEnrolledPeopleOnly(t *testing.T) {
	h, stub, read := newFaceTest(t)
	stub.set("/face/owners", http.StatusOK, `{"enrolled_count":1,"persons":[
		{"label":"alice","telegram_id":"42","photo_count":2,"photos":["1.jpg","2.jpg"],"mood_days":["2026-09-30"]},
		{"label":"unknown","photo_count":0,"photos":[]}]}`)
	if err := h.dispatchData(domain.MQTTDataCommand{Kind: domain.KindFaceOwners}); err != nil {
		t.Fatal(err)
	}
	reply := read(t, domain.KindFaceOwners, "success")
	got := dataJSON(t, reply.Data)
	// Reply data round-trips through a map, so keys come back sorted.
	want := `{"enrolled_count":1,"persons":[{"label":"alice","photo_count":2,"photos":["1.jpg","2.jpg"],"telegram_id":"42"}]}`
	if got != want {
		t.Fatalf("data = %s\nwant   %s", got, want)
	}
}

func TestFaceOwnersReportsHALFailure(t *testing.T) {
	h, stub, read := newFaceTest(t)
	stub.set("/face/owners", http.StatusServiceUnavailable, `{"detail":"Face recognizer not available"}`)
	if err := h.dispatchData(domain.MQTTDataCommand{Kind: domain.KindFaceOwners}); err != nil {
		t.Fatal(err)
	}
	if reply := read(t, domain.KindFaceOwners, "failure"); !strings.Contains(reply.Error, "503: Face recognizer not available") {
		t.Fatalf("error = %q", reply.Error)
	}
}

func TestFaceRemove(t *testing.T) {
	h, stub, read := newFaceTest(t)
	send := func(data string) {
		if err := h.dispatchData(domain.MQTTDataCommand{Kind: domain.KindFaceRemove, Data: json.RawMessage(data)}); err != nil {
			t.Fatal(err)
		}
	}

	t.Run("success", func(t *testing.T) {
		stub.set("/face/remove", http.StatusOK, `{"status":"ok","label":"alice","enrolled_count":0}`)
		send(`{"label":" alice "}`)
		read(t, domain.KindFaceRemove, "starting")
		if c := <-stub.calls; c.path != "/face/remove" || c.body["label"] != "alice" {
			t.Fatalf("HAL call = %+v", c)
		}
		reply := read(t, domain.KindFaceRemove, "success")
		if got := dataJSON(t, reply.Data); got != `{"enrolled_count":0,"label":"alice","status":"ok"}` {
			t.Fatalf("data = %s", got)
		}
	})

	t.Run("unknown person", func(t *testing.T) {
		stub.set("/face/remove", http.StatusNotFound, `{"detail":"person not found"}`)
		send(`{"label":"bob"}`)
		read(t, domain.KindFaceRemove, "starting")
		<-stub.calls
		if reply := read(t, domain.KindFaceRemove, "failure"); !strings.Contains(reply.Error, "404: person not found") {
			t.Fatalf("error = %q", reply.Error)
		}
	})

	for _, bad := range []string{`{"label":"  "}`, `{"label":"Unknown"}`, `{"label":"` + strings.Repeat("a", 65) + `"}`, `{`} {
		t.Run("rejects "+bad[:min(len(bad), 20)], func(t *testing.T) {
			send(bad)
			read(t, domain.KindFaceRemove, "failure")
			stub.noCall(t)
		})
	}
}

// The app sends face.enroll as a privacy envelope so the photo never sits on
// the broker: received ack, TLS fetch, then the normal starting/success flow.
func TestFaceEnrollViaPrivacyEnvelope(t *testing.T) {
	h, stub, read := newFaceTest(t)
	img := base64.StdEncoding.EncodeToString([]byte("jpeg-bytes"))
	stub.set("/devices/get-message", http.StatusOK, dataJSON(t, map[string]any{
		"status": 1,
		"data": map[string]any{"cmd": "data", "kind": domain.KindFaceEnroll,
			"data": map[string]any{"image_base64": img, "label": "alice"}},
	}))
	stub.set("/face/enroll", http.StatusOK, `{"status":"ok","label":"alice","photo_path":"/u/alice/1.jpg","enrolled_count":1}`)

	if err := h.HandleMessage("test/fa", []byte(`{"cmd":"data","type":"privacy","kind":"face.enroll"}`)); err != nil {
		t.Fatal(err)
	}
	read(t, domain.KindFaceEnroll, domain.MQTTStatusReceived)
	if c := <-stub.calls; c.path != "/devices/get-message" {
		t.Fatalf("first call = %s, want backend fetch", c.path)
	}
	read(t, domain.KindFaceEnroll, "starting")
	if c := <-stub.calls; c.path != "/face/enroll" || c.body["image_base64"] != img || c.body["label"] != "alice" {
		t.Fatalf("HAL call = %s %v", c.path, c.body)
	}
	read(t, domain.KindFaceEnroll, "success")
}
