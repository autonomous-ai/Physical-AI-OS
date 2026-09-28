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
// wall-clock cadence (daily/weekly/monthly) is evaluated in. Kept local to
// this package (rather than domain/device.go, the way most other kinds' data
// structs are) because the wire shape here IS exactly schedule.Schedule — a
// domain-level struct would just be a copy of it with nothing to translate.
type scheduleSyncPayload struct {
	Timezone  string              `json:"timezone"`
	Schedules []schedule.Schedule `json:"schedules"`
}

// handleScheduleSync handles kind="schedule.sync" — a FULL-STATE replace of
// the device's schedule list. The backend's list is authoritative: reconnect,
// OTA-gate-opening, and drift repair are all this same message, never a diff
// (schedules are small, so whole-list replacement costs nothing and keeps
// every code path — first sync, resync, deleting the last schedule — identical).
// Acks with the freshly computed next_run_at per schedule so the backend can
// render "Next run in 16 hours" without a second round trip.
//
// Every outcome raises one ops alert (see schedule_alerts.go): a summary of
// what the sync created and deleted, or "❌ schedule.sync — FAILED" with the
// reason. alertScheduleEvent never blocks, so the alert cannot hold up the ack.
func (h *DeviceMQTTHandler) handleScheduleSync(env domain.MQTTDataCommand) error {
	var payload scheduleSyncPayload
	if err := json.Unmarshal(env.Data, &payload); err != nil {
		msg := "invalid schedule.sync data: " + err.Error()
		h.alertScheduleEvent(scheduleSyncFailedTitle, msg)
		return h.publishDataResult(env.Kind, "failure", msg, nil)
	}

	// What was on disk BEFORE the replace, read only to diff for the alert's
	// "created: …; deleted: …". Not atomic with the replace, and it need not
	// be: the id set changes only through schedule.sync itself (the runner's
	// writes touch run bookkeeping, never which rows exist), so the worst a
	// race could do is mis-itemise one alert — never affect the sync.
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

// applyScheduleSync delegates the actual "replace store and compute next runs"
// work to schedule.SyncSchedules (see its doc comment for why that logic lives
// in the schedule package rather than here), then formats the result as the
// RFC3339 strings the ack wire format wants. Kept separate from the handler —
// which only adds JSON parsing and the fd_channel publish on top — so this is
// testable without a broker (publishDataResult needs a live MQTT client).
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
// fd_channel schedule.run ack's data payload. Split out from
// publishScheduleRunReport purely so this mapping is unit-testable without a
// live MQTT client (publishDataResult needs a real broker connection) —
// mirrors applyScheduleSync's separation from handleScheduleSync above.
//
// duration_ms is the wire contract's field name (binding — kept as-is) but
// carries rr.SendLatency: how long handing the message to the runtime took,
// NOT how long the agent's turn took (SendSystemChatMessage is fire-and-forget
// and returns immediately — see RunReport.SendLatency's doc comment).
//
// next_run_at (CRITICAL FIX, final review) is the freshly computed next
// occurrence — fire() sets RunReport.NextRunAt in the same statement it
// persists that value to the store. Without this, the backend never learned
// a schedule's next-fire time after its first run (the only other writer is
// a schedule.sync ack, which only fires on a user edit or while a row is
// still "pending"), so the web UI's cadence column got stuck on "now"
// permanently — see RunReport.NextRunAt's doc comment. Omitted (not an empty
// string) when zero: a failed fire never advances NextRunAt, and a manual
// "Run now" never touches it either, so there is nothing fresh to report.
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
//
// It used to be set for any status other than "success", which was the same
// thing while "failure" was the only other status. A "skipped" run (a
// connector in Schedule.Requires is not installed — see
// schedule.RunStatusSkipped) is deliberately not a failure, so it must not
// arrive at the backend looking like one: its reason ("missing connector:
// gmail") travels in data.summary only, and "error" stays empty: the
// stand-to-earn worker copies it verbatim into RecordScheduleRunRequest.error,
// where a non-empty value reads as a failure message on the run record.
func scheduleRunAckError(rr schedule.RunReport) string {
	if rr.Status == "failure" {
		return rr.Summary
	}
	return ""
}

// publishScheduleRunReport turns a schedule.Runner outcome into the
// fd_channel schedule.run ack. Used as the report callback for EVERY fire —
// both the ticker's automatic ones and a manual "Run now" — so the backend
// learns the outcome of a scheduled task the same way regardless of what
// triggered it. Status ("success" | "failure" | "skipped") and Summary are
// forwarded unchanged.
//
// It is also the single seam for the run's ops alert (scheduleRunAlert) —
// deliberately here and not inside the Runner, so system/schedule keeps
// knowing nothing about MQTT or alerting. The alert is raised AFTER the ack
// and never blocks (alertScheduleEvent), because this runs on the scheduler
// tick: a stuck alert endpoint must not delay the next due task.
func (h *DeviceMQTTHandler) publishScheduleRunReport(rr schedule.RunReport) {
	if err := h.publishDataResult(domain.KindScheduleRun, rr.Status, scheduleRunAckError(rr), buildScheduleRunReportData(rr)); err != nil {
		slog.Error("schedule.run: report publish failed",
			"component", "mqtt", "schedule_id", rr.ScheduleID, "error", err)
	}
	h.alertScheduleEvent(scheduleRunAlert(rr))
}

// StartScheduleRunnerLoop runs the scheduler's once-a-minute ticker until ctx
// is cancelled. Started alongside the OAuth/connector refresh loops (see
// config_watch.go's handleSetUpCompleteChange) once the device has completed
// setup.
func (h *DeviceMQTTHandler) StartScheduleRunnerLoop(ctx context.Context) {
	h.scheduleRunner.Start(ctx)
}
