package mqtthandler

import (
	"encoding/json"
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/schedule"
)

func (h *DeviceMQTTHandler) publishTimezoneSetAck(status, errMsg string, data *domain.TimezoneSetData) {
	ack := domain.MQTTTimezoneSetAck{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "data", device.GetDeviceMac()),
		Kind:             domain.KindTimezoneSet,
		Status:           status,
		Error:            errMsg,
		Data:             data,
	}
	if err := h.publish(ack); err != nil {
		slog.Warn("timezone.set: publish ack failed", "component", "mqtt", "status", status, "error", err)
	}
}

func (h *DeviceMQTTHandler) handleTimezoneSet(env domain.MQTTDataCommand) error {
	var req domain.TimezoneSetData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		slog.Error("timezone.set: invalid payload", "component", "mqtt", "error", err)
		h.publishTimezoneSetAck("failure", "invalid JSON payload", nil)
		return err
	}

	slog.Info("timezone.set: received", "component", "mqtt", "timezone", req.Timezone)

	h.publishTimezoneSetAck("starting", "", nil)

	go func() {
		if err := h.deviceService.SetTimezone(req.Timezone); err != nil {
			slog.Error("timezone.set: SetTimezone failed", "component", "mqtt", "error", err)
			h.publishTimezoneSetAck("failure", err.Error(), &req)
			return
		}
		slog.Info("timezone.set: applied", "component", "mqtt", "timezone", req.Timezone)

		h.resyncSchedulesForTimezone(req.Timezone)

		h.publishTimezoneSetAck("success", "", &req)
	}()

	return nil
}

// resyncSchedulesForTimezone re-points the schedule store at tz, recomputes
// every next run and reports them on a schedule.sync result. Best-effort.
func (h *DeviceMQTTHandler) resyncSchedulesForTimezone(timezone string) {
	changed, err := h.scheduleStore.SetTimezone(timezone)
	if err != nil {
		slog.Error("timezone.set: persist schedule timezone failed",
			"component", "mqtt", "timezone", timezone, "error", err)
		return
	}
	if !changed {
		return
	}

	existing, err := h.scheduleStore.Load()
	if err != nil {
		slog.Error("timezone.set: load schedules for recompute failed",
			"component", "mqtt", "error", err)
		return
	}
	if len(existing) == 0 {
		return
	}

	applied, nextRunAt, err := schedule.SyncSchedules(
		h.scheduleStore, existing, timezone, h.config.DeviceID, time.Now())
	if err != nil {
		slog.Error("timezone.set: recompute next runs failed",
			"component", "mqtt", "error", err)
		return
	}

	formatted := make(map[string]string, len(nextRunAt))
	for id, t := range nextRunAt {
		formatted[id] = t.Format(time.RFC3339)
	}
	slog.Info("timezone.set: schedules re-anchored",
		"component", "mqtt", "timezone", timezone, "count", applied)

	if err := h.publishDataResult(domain.KindScheduleSync, "success", "", map[string]interface{}{
		"applied":     applied,
		"next_run_at": formatted,
	}); err != nil {
		slog.Warn("timezone.set: publish recomputed next runs failed",
			"component", "mqtt", "error", err)
	}
}
