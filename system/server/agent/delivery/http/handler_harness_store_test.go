package http

import (
	"sync"
	"testing"

	"go.autonomous.ai/os/system/monitor"
)

func TestHarnessPreparationDoesNotOwnMainResponse(t *testing.T) {
	bus := monitor.ProvideBus()
	events, unsubscribe := bus.Subscribe()
	defer unsubscribe()
	h := &AgentHandler{monitorBus: bus, activeRunIDBySession: map[string]string{"session": "device-chat-42"}}
	if !h.DeliverHarnessPreparationProgress("device-chat-42", "op", "Harness needs user action.") {
		t.Fatal("active progress dropped")
	}
	event := <-events
	if event.Type != "assistant_delta" || event.RunID != "device-chat-42" || event.Summary != "Harness needs user action.\n\n" {
		t.Fatalf("event = %+v", event)
	}
	if h.suppressHarnessAgentReply("device-chat-42") {
		t.Fatal("preparation suppressed main agent guidance")
	}
	if h.DeliverHarnessPreparationProgress("other-run", "op", "wrong") {
		t.Fatal("progress crossed runs")
	}
	delete(h.activeRunIDBySession, "session")
	if h.DeliverHarnessPreparationProgress("device-chat-42", "op", "late") {
		t.Fatal("progress replaced completed main response")
	}
}

func TestHarnessPreparationCannotReplaceRemoteTaskResponse(t *testing.T) {
	h := &AgentHandler{activeRunIDBySession: map[string]string{"session": "device-chat-42"}}
	h.MarkHarnessResponseRun("device-chat-42", true, false)
	if h.DeliverHarnessPreparationProgress("device-chat-42", "op", "late ready") {
		t.Fatal("preparation replaced task progress")
	}
	if !h.DeliverHarnessResponse("device-chat-42", "Final task result") {
		t.Fatal("final dropped")
	}
	if h.DeliverHarnessPreparationProgress("device-chat-42", "op", "late ready") {
		t.Fatal("preparation replaced final")
	}
}

func TestHarnessPreparationResolvesNativeRunID(t *testing.T) {
	h := &AgentHandler{activeRunIDBySession: map[string]string{"session": "native-id"}, runIDMap: map[string]string{"native-id": "device-chat-42"}}
	if !h.DeliverHarnessPreparationProgress("device-chat-42", "op", "Preparing") {
		t.Fatal("resolved active run dropped")
	}
}

func TestHarnessPreparationDeduplicatesConcurrentPolls(t *testing.T) {
	bus := monitor.ProvideBus()
	events, unsubscribe := bus.Subscribe()
	defer unsubscribe()
	h := &AgentHandler{monitorBus: bus, activeRunIDBySession: map[string]string{"session": "run"}}
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); h.DeliverHarnessPreparationProgress("run", "op", "Launching") }()
	}
	wg.Wait()
	if event := <-events; event.Summary != "Launching\n\n" {
		t.Fatalf("event: %+v", event)
	}
	select {
	case event := <-events:
		t.Fatalf("duplicate poll appended: %+v", event)
	default:
	}
	if !h.DeliverHarnessPreparationProgress("run", "op", "Ready") {
		t.Fatal("changed progress dropped")
	}
	<-events
	if !h.DeliverHarnessPreparationProgress("run", "other-op", "Ready") {
		t.Fatal("different operation suppressed")
	}
	<-events
	delete(h.activeRunIDBySession, "session")
	h.activeRunIDBySession["new-session"] = "new-run"
	if !h.DeliverHarnessPreparationProgress("new-run", "op", "Ready") {
		t.Fatal("new run suppressed")
	}
	if len(h.harnessPreparationProgress) != 1 {
		t.Fatal("completed run progress retained")
	}
}
