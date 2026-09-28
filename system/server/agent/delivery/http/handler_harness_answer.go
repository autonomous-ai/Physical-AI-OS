package http

import (
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
)

// DeliverHarnessAnswerReceipt closes only the command's response address. An
// accepted answer belongs to the original task and is not a new task result.
// Neither accepted nor rejected receipts submit speech or modify the parent.
func (h *AgentHandler) DeliverHarnessAnswerReceipt(runID, parentRunID, state string) bool {
	if runID == "" || runID == parentRunID || (state != "completed" && state != "rejected") {
		return false
	}
	h.harnessRepliesMu.Lock()
	route, ok := h.harnessReplies[runID]
	if !ok || route.delivered || route.localOnly {
		h.harnessRepliesMu.Unlock()
		return false
	}
	route.delivered = true
	h.harnessReplies[runID] = route
	h.harnessRepliesMu.Unlock()
	sensinghttp.DefaultFillerManager.Cancel(runID)
	text := "Answer sent."
	if state == "rejected" {
		text = "Harness did not accept the answer."
	} else if parentRunID != "" {
		text = "Answer sent. Results belong to the original task."
	}
	details := map[string]string{"role": "assistant", "message": text, "source": "harness", "command": "question.answer", "receipt_state": state}
	record := map[string]any{"run_id": runID, "text": text, "source": "harness", "command": "question.answer", "receipt_state": state}
	if parentRunID != "" {
		details["original_run_id"] = parentRunID
		record["original_run_id"] = parentRunID
	}
	flow.Log("harness_response", record, runID)
	if h.monitorBus != nil {
		h.monitorBus.Push(domain.MonitorEvent{Type: "chat_response", Summary: text, RunID: runID, State: "final", Detail: details})
	}
	return true
}
