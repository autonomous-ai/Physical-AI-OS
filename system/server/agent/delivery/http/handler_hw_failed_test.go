package http

import (
	"errors"
	"net/http"
	"testing"

	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
)

// failingTransport fails every request at the transport, the same path as a client timeout.
type failingTransport struct{}

func (failingTransport) RoundTrip(*http.Request) (*http.Response, error) {
	return nil, errors.New("context deadline exceeded (Client.Timeout exceeded while awaiting headers)")
}

// A transport-failed hardware POST must still log a flow event (#342 defects H, K).
func TestATransportFailureIsLoggedAsAFlowEvent(t *testing.T) {
	// No flow.Init: it would start the JSONL writer in the test's working directory.
	h := &AgentHandler{
		monitorBus: monitor.ProvideBus(),
		config:     &config.Config{DeviceType: "lamp"},
	}
	before := len(flow.Recent(1000))

	ok := h.fireHWCall(hwCall{path: "/servo/search", body: `{"target":"keyboard"}`},
		"device-chat-44", &http.Client{Transport: failingTransport{}})

	if ok {
		t.Fatal("a failed POST reported success")
	}
	var found *flow.Event
	for _, ev := range flow.Recent(1000)[before:] {
		if ev.Node == "hw_failed" {
			found = &ev
			break
		}
	}
	if found == nil {
		t.Fatal("no hw_failed event — the failure is invisible to the monitor")
	}
	if found.TraceID != "device-chat-44" {
		t.Errorf("hw_failed carries run %q, want device-chat-44", found.TraceID)
	}
	if found.Data["path"] != "/servo/search" {
		t.Errorf("hw_failed path = %v, want /servo/search", found.Data["path"])
	}
	if e, _ := found.Data["error"].(string); e == "" {
		t.Error("hw_failed carries no error text — a reader cannot tell a timeout from a refused connection")
	}
}
