package http

import (
	"testing"

	"go.autonomous.ai/os/system/monitor"
)

func TestHarnessAnswerReceiptClosesCommandWithoutConsumingOriginalTask(t *testing.T) {
	bus := monitor.ProvideBus()
	events, unsubscribe := bus.Subscribe()
	defer unsubscribe()
	h := &AgentHandler{monitorBus: bus}
	// Voice addresses exercise the display-only command path as well as web.
	h.MarkHarnessResponseRun("task-a", false, false)
	h.MarkHarnessResponseRun("answer-b", false, false)
	if !h.DeliverHarnessAnswerReceipt("answer-b", "task-a", "completed") {
		t.Fatal("answer not delivered")
	}
	event := <-events
	if event.RunID != "answer-b" || event.Type != "chat_response" || event.State != "final" || event.Summary != "Answer sent. Results belong to the original task." {
		t.Fatalf("wrong event: %#v", event)
	}
	details := event.Detail.(map[string]string)
	if details["source"] != "harness" || details["command"] != "question.answer" || details["receipt_state"] != "completed" || details["original_run_id"] != "task-a" {
		t.Fatalf("wrong command metadata: %v", details)
	}
	if _, ok := details["result_id"]; ok {
		t.Fatal("invented result")
	}
	if _, ok := details["outcome"]; ok {
		t.Fatal("invented task outcome")
	}
	if h.harnessReplies["task-a"].delivered || !h.harnessReplies["answer-b"].delivered {
		t.Fatal("wrong route consumed")
	}
	if h.DeliverHarnessAnswerReceipt("answer-b", "task-a", "completed") {
		t.Fatal("duplicate accepted")
	}
	select {
	case extra := <-events:
		t.Fatalf("duplicate notification: %#v", extra)
	default:
	}
}

func TestHarnessAnswerReceiptStatesAndUnlinkedCommand(t *testing.T) {
	for _, test := range []struct{ state, parent, want string }{
		{"completed", "", "Answer sent."},
		{"rejected", "task-a", "Harness did not accept the answer."},
	} {
		t.Run(test.state, func(t *testing.T) {
			bus := monitor.ProvideBus()
			events, unsubscribe := bus.Subscribe()
			defer unsubscribe()
			h := &AgentHandler{monitorBus: bus}
			h.MarkHarnessResponseRun("answer-b", true, false)
			if !h.DeliverHarnessAnswerReceipt("answer-b", test.parent, test.state) {
				t.Fatal("missing command receipt")
			}
			event := <-events
			if event.Summary != test.want {
				t.Fatalf("wrong text: %q", event.Summary)
			}
			details := event.Detail.(map[string]string)
			if details["receipt_state"] != test.state {
				t.Fatal(details)
			}
			if test.parent == "" {
				if _, ok := details["original_run_id"]; ok {
					t.Fatal("invented parent")
				}
			}
		})
	}
}

func TestHarnessAnswerReceiptRejectsUnknownPendingOrLocal(t *testing.T) {
	bus := monitor.ProvideBus()
	events, unsubscribe := bus.Subscribe()
	defer unsubscribe()
	h := &AgentHandler{monitorBus: bus}
	h.MarkHarnessResponseRun("answer-b", false, false)
	h.MarkHarnessLocalResponseRun("local", false)
	for _, test := range []struct{ id, state string }{{"", "completed"}, {"unknown", "rejected"}, {"local", "completed"}, {"answer-b", "delivered"}, {"answer-b", "queued"}, {"answer-b", "unknown"}} {
		if h.DeliverHarnessAnswerReceipt(test.id, "task-a", test.state) {
			t.Fatalf("invalid receipt accepted: %+v", test)
		}
	}
	if h.DeliverHarnessAnswerReceipt("answer-b", "answer-b", "completed") {
		t.Fatal("answer receipt consumed its own original task")
	}
	if h.harnessReplies["answer-b"].delivered {
		t.Fatal("nonterminal receipt closed command")
	}
	select {
	case event := <-events:
		t.Fatalf("invalid notification: %#v", event)
	default:
	}
}
