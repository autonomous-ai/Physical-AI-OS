package mqtthandler

import (
	"encoding/json"
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/schedule"
)

// scheduleMutatePayload is the "data" object of an outbound schedule.mutate.
type scheduleMutatePayload struct {
	IntentID   string                  `json:"intent_id"`
	Op         string                  `json:"op"`
	ScheduleID string                  `json:"schedule_id,omitempty"`
	BaseRev    uint64                  `json:"base_rev,omitempty"`
	Schedule   *schedule.IntentPayload `json:"schedule,omitempty"`
}

// scheduleMutateAck is the inbound verdict for one proposal.
type scheduleMutateAck struct {
	IntentID string `json:"intent_id"`
	Applied  bool   `json:"applied"`
	Conflict bool   `json:"conflict"`
	Reason   string `json:"reason,omitempty"`
}

// publishIntent sends one queued intent on the fd_channel and stamps the
// attempt. A failed publish must NOT drop the intent; it is retried.
func (h *DeviceMQTTHandler) publishIntent(in schedule.Intent) {
	payload := scheduleMutatePayload{
		IntentID:   in.IntentID,
		Op:         in.Op,
		ScheduleID: in.ScheduleID,
		BaseRev:    in.BaseRev,
		Schedule:   in.Payload,
	}
	if err := h.publishDataResult(domain.KindScheduleMutate, "success", "", payload); err != nil {
		slog.Error("schedule: publish mutate failed", "component", "schedule",
			"intent_id", in.IntentID, "op", in.Op, "error", err)
		return
	}
	if err := h.scheduleIntents.MarkSent(in.IntentID, time.Now()); err != nil {
		slog.Warn("schedule: mark intent sent failed", "component", "schedule",
			"intent_id", in.IntentID, "error", err)
	}
	slog.Info("schedule: mutate published", "component", "schedule",
		"intent_id", in.IntentID, "op", in.Op, "schedule_id", in.ScheduleID, "base_rev", in.BaseRev)
}

// FlushScheduleIntents republishes every queued intent. Safe to repeat: the
// backend collapses replays on intent_id.
func (h *DeviceMQTTHandler) FlushScheduleIntents() {
	if h.scheduleIntents == nil {
		return
	}
	pending := h.scheduleIntents.List()
	if len(pending) == 0 {
		return
	}
	slog.Info("schedule: flushing queued intents", "component", "schedule", "count", len(pending))
	for _, in := range pending {
		h.publishIntent(in)
	}
}

// handleScheduleMutateAck records the backend's verdict on one proposal. The
// intent is dropped on either outcome: a rejected base_rev never becomes valid.
func (h *DeviceMQTTHandler) handleScheduleMutateAck(env domain.MQTTDataCommand) error {
	var ack scheduleMutateAck
	if err := json.Unmarshal(env.Data, &ack); err != nil {
		slog.Error("schedule: bad mutate ack payload", "component", "schedule", "error", err)
		return err
	}
	if ack.IntentID == "" {
		slog.Warn("schedule: mutate ack without intent_id, ignoring", "component", "schedule")
		return nil
	}
	if err := h.scheduleIntents.Remove(ack.IntentID); err != nil {
		slog.Error("schedule: failed to clear acked intent", "component", "schedule",
			"intent_id", ack.IntentID, "error", err)
		return err
	}
	slog.Info("schedule: mutate ack", "component", "schedule",
		"intent_id", ack.IntentID, "applied", ack.Applied,
		"conflict", ack.Conflict, "reason", ack.Reason)
	return nil
}
