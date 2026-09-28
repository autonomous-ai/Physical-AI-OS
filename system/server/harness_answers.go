package server

import (
	"encoding/json"
	"log/slog"

	"go.autonomous.ai/os/system/harness"
)

// Only explicit task correlation can bind a question. App-origin or grouped
// questions without a singular original input never borrow the newest task.
func (s *Server) captureHarnessQuestion(frame harness.Frame, peer harness.ResultContext) error {
	if !s.acceptsHarnessResults(peer) {
		return nil
	}
	payload, ok := frame["payload"].(map[string]any)
	if !ok {
		return nil
	}
	agentID, _ := frame["agentId"].(string)
	questionID, _ := payload["questionRequestId"].(string)
	key, _ := payload["idempotencyKey"].(string)
	if key == "" {
		key, _ = frame["idempotencyKey"].(string)
	}
	if questionID == "" || key == "" {
		return nil
	}
	for _, in := range s.harnessResults.Inputs() {
		if in.ResultID == "" && in.Owner == peer.Owner && in.ServerInstanceID == peer.ServerInstanceID && in.AgentID == agentID && in.IdempotencyKey == key {
			return s.harnessResults.BindQuestion(in.Owner, in.ServerInstanceID, in.AgentID, questionID, key)
		}
	}
	return nil
}

// Command receipts close the answer's response address only. They are neither
// task results nor permission to speak; the original input owns its summary.
// Caller holds harnessResultsMu, shared with final-result publication.
func (s *Server) deliverHarnessAnswerReceipts(peer harness.ResultContext) {
	if s.agentHandler == nil {
		return
	}
	for _, answer := range s.harnessResults.Answers() {
		in := answer.Input
		if in.Owner != peer.Owner || (answer.ReceiptState != "completed" && answer.ReceiptState != "rejected") {
			continue
		}
		identity, _ := json.Marshal([]string{in.Owner, in.ServerInstanceID, in.IdempotencyKey})
		ref := "answer:" + string(identity)
		if s.harnessResultsPublished[ref] {
			continue
		}
		message := "Harness did not accept the answer."
		if answer.ReceiptState == "completed" {
			message = "Answer sent."
			if answer.Parent.RunID != "" {
				message += " Results belong to original task " + answer.Parent.RunID + "."
			}
		}
		if err := s.completeHarnessHistory(in.RunID, message); err != nil {
			slog.Warn("Harness answer history pending", "error", err)
			continue
		}
		s.restoreHarnessResultRoute(in)
		s.harnessRepliesMu.Lock()
		route, found := s.harnessReplies[in.RunID]
		matched := found && route.agentID == in.AgentID && route.idempotencyKey == in.IdempotencyKey
		if matched {
			route.answer = true
			s.harnessReplies[in.RunID] = route
		}
		s.harnessRepliesMu.Unlock()
		if !matched {
			continue
		}
		s.agentHandler.DeliverHarnessAnswerReceipt(in.RunID, answer.Parent.RunID, answer.ReceiptState)
		s.forgetHarnessReply(in.AgentID, in.RunID)
		if s.harnessResultsPublished == nil {
			s.harnessResultsPublished = map[string]bool{}
		}
		s.harnessResultsPublished[ref] = true
	}
}
