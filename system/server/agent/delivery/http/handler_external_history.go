package http

import (
	"encoding/json"

	"go.autonomous.ai/os/system/domain"
)

// SetExternalHistoryObserver registers an observer for turn completion only; delivery is unchanged.
func (h *AgentHandler) SetExternalHistoryObserver(fn func(runID string, failed bool)) {
	h.externalHistoryObserver = fn
}

func (h *AgentHandler) observeExternalHistory(evt domain.WSEvent) {
	if h.externalHistoryObserver == nil {
		return
	}
	var p struct {
		RunID      string `json:"runId"`
		SnakeRunID string `json:"run_id"`
		Stream     string `json:"stream"`
		State      string `json:"state"`
		Data       struct {
			Phase   string          `json:"phase"`
			Error   json.RawMessage `json:"error"`
			Aborted bool            `json:"aborted"`
		} `json:"data"`
	}
	if json.Unmarshal(evt.Payload, &p) != nil {
		return
	}
	if p.RunID == "" {
		p.RunID = p.SnakeRunID
	}
	if p.RunID == "" {
		return
	}
	phase := ""
	if evt.Event == "agent" && p.Stream == "lifecycle" {
		phase = p.Data.Phase
	}
	if evt.Event == "chat" {
		switch p.State {
		case "error", "aborted":
			phase = "error"
		}
	}
	if phase != "end" && phase != "error" {
		return
	}
	failed := phase == "error" || p.Data.Aborted || (len(p.Data.Error) > 0 && string(p.Data.Error) != "null" && string(p.Data.Error) != `""`)
	h.externalHistoryObserver(h.resolveRunID(p.RunID), failed)
}
