package http

import "go.autonomous.ai/os/system/domain"

// DeliverHarnessPreparationProgress displays a polled Store snapshot while its
// original main-agent turn is active. It never takes response ownership or
// speaks: main-agent guidance remains available even when preparation fails.
func (h *AgentHandler) DeliverHarnessPreparationProgress(runID, operationID, text string) bool {
	if runID == "" || text == "" {
		return false
	}
	h.agentLifecycleMu.Lock()
	runs := make([]string, 0, len(h.activeRunIDBySession))
	for _, id := range h.activeRunIDBySession {
		runs = append(runs, id)
	}
	h.agentLifecycleMu.Unlock()
	activeRuns := make(map[string]bool, len(runs))
	for _, id := range runs {
		activeRuns[h.resolveRunID(id)] = true
	}
	if !activeRuns[runID] {
		return false
	}
	// Serialize against remote final delivery and refuse progress once a task
	// owns this response, so a late preparation poll cannot replace its result.
	h.harnessRepliesMu.Lock()
	defer h.harnessRepliesMu.Unlock()
	if _, ownsReply := h.harnessReplies[runID]; ownsReply {
		return false
	}
	// Bound retained progress to active turns. Polls with identical visible
	// content are not assistant token deltas and must not append repeatedly.
	for id := range h.harnessPreparationProgress {
		if !activeRuns[id] {
			delete(h.harnessPreparationProgress, id)
		}
	}
	key := operationID + "\x00" + text
	if h.harnessPreparationProgress[runID] == key {
		return false
	}
	if h.monitorBus != nil {
		if h.harnessPreparationProgress == nil {
			h.harnessPreparationProgress = make(map[string]string)
		}
		h.harnessPreparationProgress[runID] = key
		h.monitorBus.Push(domain.MonitorEvent{
			Type: "assistant_delta", Summary: text + "\n\n", RunID: runID,
			Detail: map[string]string{"role": "assistant", "source": "harness"},
		})
	}
	return true
}
