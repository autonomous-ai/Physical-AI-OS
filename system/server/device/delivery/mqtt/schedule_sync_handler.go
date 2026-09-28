package mqtthandler

import (
	"context"
	"encoding/json"
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/schedule"
)

// scheduleSyncPayload is the Data payload for kind:"schedule.sync" — the
// backend's full authoritative task list plus the device-wide timezone every
// wall-clock cadence (daily/weekly/monthly) is evaluated in.
type scheduleSyncPayload struct {
	Timezone  string              `json:"timezone"`
	Schedules []schedule.Schedule `json:"schedules"`
}

// handleScheduleSync handles kind="schedule.sync" — a FULL-STATE replace of
// the device's schedule list.
func (h *DeviceMQTTHandler) handleScheduleSync(env domain.MQTTDataCommand) error {
	var payload scheduleSyncPayload
	if err := json.Unmarshal(env.Data, &payload); err != nil {
		msg := "invalid schedule.sync data: " + err.Error()
		h.alertScheduleEvent(scheduleSyncFailedTitle, msg)
		return h.publishDataResult(env.Kind, "failure", msg, nil)
	}

	// Read only to diff for the alert; not atomic with the replace, which is
	// fine (a race can only mis-itemise one alert).
	prior, _ := h.scheduleStore.Load()

	applied, nextRunAt, err := applyScheduleSync(h.scheduleStore, payload, h.config.DeviceID, time.Now())
	if err != nil {
		msg := "store: " + err.Error()
		h.alertScheduleEvent(scheduleSyncFailedTitle, msg)
		return h.publishDataResult(env.Kind, "failure", msg, nil)
	}

	slog.Info("schedule.sync: applied", "component", "mqtt", "count", applied)
	h.alertScheduleEvent(scheduleSyncAlertTitle(applied, prior, payload.Schedules), "")
	return h.publishDataResult(env.Kind, "success", "", map[string]interface{}{
		"applied":     applied,
		"next_run_at": nextRunAt,
	})
}

// applyScheduleSync replaces the store via schedule.SyncSchedules and returns
// next runs as RFC3339 strings for the ack.
func applyScheduleSync(store *schedule.Store, payload scheduleSyncPayload, deviceID string, now time.Time) (applied int, nextRunAt map[string]string, err error) {
	applied, computed, err := schedule.SyncSchedules(store, payload.Schedules, payload.Timezone, deviceID, now)
	if err != nil {
		return 0, nil, err
	}
	nextRunAt = make(map[string]string, len(computed))
	for id, t := range computed {
		nextRunAt[id] = t.Format(time.RFC3339)
	}
	return applied, nextRunAt, nil
}

// buildScheduleRunReportData turns a schedule.Runner outcome into the
// fd_channel schedule.run ack's data payload.
func buildScheduleRunReportData(rr schedule.RunReport) map[string]interface{} {
	data := map[string]interface{}{
		"id":          rr.ScheduleID,
		"run_id":      rr.RunID,
		"started_at":  rr.StartedAt.Format(time.RFC3339),
		"duration_ms": rr.SendLatency.Milliseconds(),
		"summary":     rr.Summary,
	}
	if !rr.NextRunAt.IsZero() {
		data["next_run_at"] = rr.NextRunAt.Format(time.RFC3339)
	}
	return data
}

// scheduleRunAckError is the schedule.run ack's top-level "error": the
// summary of a FAILED run, and empty for anything else.
func scheduleRunAckError(rr schedule.RunReport) string {
	if rr.Status == "failure" {
		return rr.Summary
	}
	return ""
}

// publishScheduleRunReport turns a schedule.Runner outcome into the
// fd_channel schedule.run ack.
func (h *DeviceMQTTHandler) publishScheduleRunReport(rr schedule.RunReport) {
	if err := h.publishDataResult(domain.KindScheduleRun, rr.Status, scheduleRunAckError(rr), buildScheduleRunReportData(rr)); err != nil {
		slog.Error("schedule.run: report publish failed",
			"component", "mqtt", "schedule_id", rr.ScheduleID, "error", err)
	}
	h.alertScheduleEvent(scheduleRunAlert(rr))
}

// StartScheduleRunnerLoop runs the scheduler's once-a-minute ticker until ctx
// is cancelled.
func (h *DeviceMQTTHandler) StartScheduleRunnerLoop(ctx context.Context) {
	h.scheduleRunner.Start(ctx)
}
