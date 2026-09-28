package mqtthandler

import (
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

func TestForwardChatLocalIntentReplaysFinal(t *testing.T) {
	for _, speak := range []bool{false, true} {
		name := "typed"
		if speak {
			name = "spoken"
		}
		t.Run(name, func(t *testing.T) {
			stream, sent := newTestStream()
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var request sensingRequest
				if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
					t.Error(err)
				}
				wantType := "mqtt_chat"
				if speak {
					wantType = "voice"
				}
				if request.Type != wantType || request.Message != "Make this lamp violet." {
					t.Errorf("unexpected request: %+v", request)
				}
				_, _ = w.Write([]byte(`{"status":1,"data":{"handler":"local","handledLocally":"true","localRunId":"local-intent-123","response":"Done."}}`))
			}))
			defer server.Close()
			h := &DeviceMQTTHandler{config: &config.Config{HttpPort: server.Listener.Addr().(*net.TCPAddr).Port}, chatStream: stream}
			finish := stream.capture()
			defer finish("", "")
			runID, err := h.forwardChatToSensing(domain.MQTTChatSendData{Message: "Make this lamp violet.", Speak: speak})
			if err != nil || runID != "local-intent-123" {
				t.Fatalf("runID=%q error=%v", runID, err)
			}
			if len(sent()) != 0 {
				t.Fatal("local reply escaped capture before session correlation")
			}
			finish(runID, "phone-session")
			finish("", "")
			events := sent()
			if len(events) != 1 {
				t.Fatalf("expected exactly one final reply, got %+v", events)
			}
			event := events[0]
			if event.RunID != runID || event.SessionID != "phone-session" || event.Event.Type != "chat_response" || event.Event.State != "final" || event.Event.Summary != "Done." {
				t.Fatalf("incorrect local reply: %+v", event)
			}
			if len(stream.runs) != 0 || len(stream.captures) != 0 {
				t.Fatal("completed local turn leaked tracking state")
			}
		})
	}
}

func TestForwardChatRequiresCorrelatableRun(t *testing.T) {
	for _, tc := range []struct {
		name, data, want string
	}{
		{"agent", `{"runId":"agent-run"}`, "agent-run"},
		{"missing local ID", `{"handler":"local","handledLocally":"true"}`, ""},
		{"unmarked local ID", `{"localRunId":"local-run"}`, ""},
		{"rejected local marker", `{"handler":"local","handledLocally":"false","localRunId":"local-run"}`, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_, _ = w.Write([]byte(`{"status":1,"data":` + tc.data + `}`))
			}))
			defer server.Close()
			h := &DeviceMQTTHandler{config: &config.Config{HttpPort: server.Listener.Addr().(*net.TCPAddr).Port}}
			runID, err := h.forwardChatToSensing(domain.MQTTChatSendData{Message: "Hello"})
			if runID != tc.want || (err != nil) != (tc.want == "") {
				t.Fatalf("runID=%q error=%v, want runID=%q", runID, err, tc.want)
			}
		})
	}
}
