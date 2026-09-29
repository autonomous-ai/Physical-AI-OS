package http

// correlateTaskRun uses optional runtime evidence without changing the shared
// run mapping used for speech suppression, chat delivery, or dispatch.
func (h *AgentHandler) correlateTaskRun(backendRunID, message string) {
	matcher, ok := h.agentGateway.(interface{ MatchPendingTaskByMessage(string) string })
	if !ok {
		return
	}
	h.taskRunIDsMu.Lock()
	defer h.taskRunIDsMu.Unlock()
	if _, exists := h.taskRunIDs[backendRunID]; exists {
		return
	}
	deviceRunID := matcher.MatchPendingTaskByMessage(message)
	if deviceRunID == "" {
		// Do not promote the routing matcher's FIFO/prefix guess to metric evidence.
		deviceRunID = backendRunID
	}
	if h.taskRunIDs == nil {
		h.taskRunIDs = make(map[string]string)
	}
	if len(h.taskRunIDs) >= 1024 {
		// Evict rather than retain unbounded; eviction affects only metrics, never runtime behavior.
		for key := range h.taskRunIDs {
			delete(h.taskRunIDs, key)
			break
		}
	}
	h.taskRunIDs[backendRunID] = deviceRunID
}

func (h *AgentHandler) resolveTaskRunID(backendRunID, fallback string) string {
	h.taskRunIDsMu.Lock()
	defer h.taskRunIDsMu.Unlock()
	if runID := h.taskRunIDs[backendRunID]; runID != "" {
		return runID
	}
	if _, ok := h.agentGateway.(interface{ MatchPendingTaskByMessage(string) string }); ok {
		return backendRunID
	}
	return fallback
}
