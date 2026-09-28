package server

import (
	"errors"
	"strings"

	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/telemetry"
)

func reportHarnessDispatchError(runID, interactionID string, err error) {
	if err == nil {
		return
	}
	var uncertain *harness.DeliveryUnknownError
	if errors.As(err, &uncertain) {
		telemetry.ReportTaskExecution(runID, interactionID, "unknown", "execution_observation_lost")
		return
	}
	telemetry.ReportTaskExecution(runID, interactionID, "failed", "dispatch_error")
}

// observeHarnessExecution is independent of text delivery: done needs no recap,
// errors need no display text, and asking a question is not execution success.
func (s *Server) observeHarnessExecution(agentID, kind string, frame harness.Frame) {
	var outcome, evidence string
	switch kind {
	case "turn.done":
		return // Lifecycle end alone does not establish result membership.
	case "turn.summary":
		if strings.TrimSpace(harnessEventText(kind, frame)) == "" {
			return
		}
		outcome, evidence = "completed", "harness_turn_summary"
	case "turn.error", "agent.error":
		outcome, evidence = "failed", "harness_turn_error"
	case "question.open":
		outcome, evidence = "unknown", "harness_question_open"
	default:
		return
	}
	s.harnessRepliesMu.Lock()
	reply, ok := s.harnessReplyForFrameLocked(agentID, frame)
	s.harnessRepliesMu.Unlock()
	runID := ""
	if ok {
		runID = reply.runID
	}

	if runID != "" {
		telemetry.ReportTaskExecution(runID, "", outcome, evidence)
	}
}

// VoiceController uses this callback when an answer is stored locally and it
// must ask another structured question before dispatching anything remotely.
func (s *Server) deliverHarnessVoiceQuestion(agentID, runID, text string) {
	telemetry.ReportTaskExecution(runID, "", "unknown", "harness_question_open")
	s.deliverHarnessVoiceMessage(agentID, runID, text)
}

// A rejection is definitive dispatch failure. Other receipt states only
// acknowledge the command; execution completion still needs lifecycle evidence.
func reportHarnessReceipt(runID string, frame harness.Frame) {
	receipt, _ := frame["receipt"].(map[string]any)
	if state, _ := receipt["state"].(string); state == "rejected" {
		telemetry.ReportTaskExecution(runID, "", "failed", "dispatch_error")
	}
}
