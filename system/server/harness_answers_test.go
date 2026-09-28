package server

import (
	"go.autonomous.ai/os/system/harness"
	"testing"
)

func reserveServerAnswer(t *testing.T, s *Server, p harness.ResultContext, linked bool) harness.Frame {
	t.Helper()
	if linked {
		question := harness.Frame{"type": "event", "kind": "question.open", "agentId": "agent", "payload": map[string]any{"questionRequestId": "q", "idempotencyKey": "a"}}
		if err := s.captureHarnessResult(question, p); err != nil {
			t.Fatal(err)
		}
	}
	frame := harness.Frame{"type": "question.answer", "agentId": "agent", "idempotencyKey": "b", "questionRequestId": "q", "answers": map[string]any{"color": "black"}}
	s.registerHarnessDispatch("agent", "run-b", true, true, frame)
	if err := s.reserveHarnessResult(frame, p); err != nil {
		t.Fatal(err)
	}
	return frame
}
func TestHarnessAnswerReceiptClosesOnlyCommand(t *testing.T) {
	for _, state := range []string{"completed", "rejected"} {
		t.Run(state, func(t *testing.T) {
			s, p := resultServer(t)
			reserveServerInput(t, s, p, "a")
			frame := reserveServerAnswer(t, s, p, true)
			if err := s.reserveHarnessResult(frame, p); err != nil {
				t.Fatal(err)
			}
			if len(s.harnessResults.Inputs()) != 1 || len(s.harnessResults.Answers()) != 1 {
				t.Fatal("answer became task input")
			}
			if s.harnessReplies["run-a"].overlapped {
				t.Fatal("answer made original task ambiguous")
			}
			s.processHarnessResults(p)
			if !s.hasHarnessReply("agent", "run-b") {
				t.Fatal("unconfirmed answer closed")
			}
			receipt := serverReceipt("b")
			receipt["receipt"].(map[string]any)["state"] = state
			s.bindHarnessResultReceipt(receipt, p)
			s.processHarnessResults(p)
			if s.hasHarnessReply("agent", "run-b") || !s.hasHarnessReply("agent", "run-a") {
				t.Fatal("wrong command/task closure")
			}
			if len(s.harnessResults.Results()) != 0 {
				t.Fatal("receipt invented final result")
			}
			s.bindHarnessResultReceipt(serverReceipt("a"), p)
			if err := s.captureHarnessResult(serverGroup("a"), p); err != nil {
				t.Fatal(err)
			}
			s.processHarnessResults(p)
			if s.hasHarnessReply("agent", "run-a") || len(s.harnessResults.Results()) != 1 {
				t.Fatal("original summary did not finish")
			}
			s.processHarnessResults(p)
			if s.hasHarnessReply("agent", "run-b") {
				t.Fatal("answer replay restored pending route")
			}
		})
	}
}
func TestHarnessAnswerSummaryBeforeReceipt(t *testing.T) {
	s, p := resultServer(t)
	reserveServerInput(t, s, p, "a")
	reserveServerAnswer(t, s, p, true)
	s.bindHarnessResultReceipt(serverReceipt("a"), p)
	if err := s.captureHarnessResult(serverGroup("a"), p); err != nil {
		t.Fatal(err)
	}
	s.processHarnessResults(p)
	if !s.hasHarnessReply("agent", "run-b") {
		t.Fatal("summary completed answer command")
	}
	receipt := serverReceipt("b")
	receipt["receipt"].(map[string]any)["state"] = "completed"
	s.bindHarnessResultReceipt(receipt, p)
	s.processHarnessResults(p)
	if s.hasHarnessReply("agent", "run-b") || len(s.harnessResults.Results()) != 1 {
		t.Fatal("late answer ACK failed")
	}
}
func TestHarnessUnlinkedAnswerAndRestart(t *testing.T) {
	s, p := resultServer(t)
	reserveServerAnswer(t, s, p, false)
	if s.harnessResults.Answers()[0].Parent.RunID != "" {
		t.Fatal("invented parent")
	}
	receipt := serverReceipt("b")
	receipt["receipt"].(map[string]any)["state"] = "completed"
	s.bindHarnessResultReceipt(receipt, p)
	// Simulate lost RAM routes; durable command receipt restores display only.
	s.harnessReplies = nil
	s.processHarnessResults(p)
	if s.hasHarnessReply("agent", "run-b") || len(s.harnessResults.Inputs()) != 0 {
		t.Fatal("unlinked answer waits for impossible result")
	}
}
