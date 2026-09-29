package mqtthandler

import (
	"encoding/json"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/schedule"
)

// scheduleRunPayload is the Data payload for kind:"schedule.run" — the "Run
// now" button.
type scheduleRunPayload struct {
	ID string `json:"id"`
}

// handleScheduleRun handles kind="schedule.run" — fires ONE stored schedule
// immediately through the same Runner the ticker uses, so a manual run and a
// scheduled one report identically over fd_channel.
func (h *DeviceMQTTHandler) handleScheduleRun(env domain.MQTTDataCommand) error {
	var req scheduleRunPayload
	if err := json.Unmarshal(env.Data, &req); err != nil {
		return h.publishDataResult(env.Kind, "failure", "invalid schedule.run data: "+err.Error(), nil)
	}

	sch, errMsg := resolveScheduleRun(h.scheduleStore, req.ID)
	if errMsg != "" {
		return h.publishDataResult(env.Kind, "failure", errMsg, nil)
	}

	if _, ran := h.scheduleRunner.RunNow(sch); !ran {
		// RunNow never invoked SendSystemChatMessage in this branch, so
		// publishScheduleRunReport was never called; this is the one place
		// that must still ack the request.
		return h.publishDataResult(env.Kind, "failure", "agent busy, try again shortly",
			map[string]interface{}{"id": sch.ID})
	}
	return nil
}

// resolveScheduleRun looks up id in the store, returning a non-empty message
// when the request can't be run at all (blank id, unknown id).
func resolveScheduleRun(store *schedule.Store, id string) (schedule.Schedule, string) {
	id = strings.TrimSpace(id)
	if id == "" {
		return schedule.Schedule{}, "id is required"
	}
	sch, ok := store.Get(id)
	if !ok {
		return schedule.Schedule{}, "unknown schedule id: " + id
	}
	return sch, ""
}
