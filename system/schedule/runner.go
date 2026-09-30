package schedule

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
)

const (
	runnerTickInterval = 1 * time.Minute

	// runnerCatchUpWindow: an occurrence overdue by more than this is re-anchored forward,
	// never fired, so a device booting after a night off does not burst missed runs.
	runnerCatchUpWindow = 30 * time.Minute
)

const (
	// RunStatusSkipped marks a run not performed because a required connector is missing (wire value).
	RunStatusSkipped = "skipped"

	// missingConnectorSummaryPrefix starts a skip Summary, e.g. "missing connector: gmail, slack" (parsed by the web).
	missingConnectorSummaryPrefix = "missing connector: "
)

// RunReport is passed to the report callback after every fire attempt (ticker or manual).
type RunReport struct {
	ScheduleID string
	// Name is the schedule name at run time, in every outcome.
	Name string
	// Manual is true for RunNow, false for ticker fires.
	Manual bool
	// RunID is the run id returned by the gateway; empty on failure, skip and KindSpeak.
	RunID     string
	StartedAt time.Time
	// SendLatency is the hand-off latency (send/accept), not turn or speech duration.
	SendLatency time.Duration

	// Status is "success" | "failure" | "skipped". Success means handed off, not completed or heard;
	// skipped is not a failure (no retry, NextRunAt advances).
	Status string
	// Summary is the Name on success, the error on failure, or the missing-connector list on skip.
	Summary string
	// NextRunAt is the next occurrence persisted on success/skip (zero otherwise).
	// It must ride on the ack: the backend has no other source for the post-run next time.
	NextRunAt time.Time
}

// Runner fires due schedules once a minute through domain.AgentGateway (SendSystemChatMessage or Speak).
type Runner struct {
	store    *Store
	gw       domain.AgentGateway
	deviceID string
	report   func(RunReport)

	// connectors backs the Schedule.Requires guard; nil disables it.
	connectors ConnectorChecker
}

// ConnectorChecker reports whether a connector code has credentials on this device.
// Answer false only on positive evidence of absence; when unknown, answer true.
type ConnectorChecker interface {
	Installed(code string) bool
}

// ConnectorCheckerFunc adapts a function to ConnectorChecker.
type ConnectorCheckerFunc func(code string) bool

// Installed calls f(code).
func (f ConnectorCheckerFunc) Installed(code string) bool { return f(code) }

// SetConnectorChecker enables the Schedule.Requires guard. Call before Start; not synchronized.
func (r *Runner) SetConnectorChecker(c ConnectorChecker) { r.connectors = c }

// NewRunner builds a Runner. deviceID seeds schedule jitter; report may be nil.
func NewRunner(store *Store, gw domain.AgentGateway, deviceID string, report func(RunReport)) *Runner {
	return &Runner{store: store, gw: gw, deviceID: deviceID, report: report}
}

// Start runs the ticker loop until ctx is cancelled; the first tick runs immediately (boot catch-up).
func (r *Runner) Start(ctx context.Context) {
	r.safeTick(time.Now())
	ticker := time.NewTicker(runnerTickInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			r.safeTick(now)
		}
	}
}

// safeTick runs one tick, recovering from panics so the loop survives.
func (r *Runner) safeTick(now time.Time) {
	defer func() {
		if rec := recover(); rec != nil {
			slog.Error("schedule: panic in runner tick", "component", "schedule", "panic", rec)
		}
	}()
	r.tick(now)
}

// tick fires due schedules one at a time, deferring any while the gateway is busy.
// Skips bypass the busy check: they never touch the gateway.
func (r *Runner) tick(now time.Time) {
	schedules, err := r.store.Load()
	if err != nil {
		slog.Error("schedule: load failed", "component", "schedule", "error", err)
		return
	}
	tz := r.store.Timezone()

	for _, sch := range schedules {
		if !sch.Enabled || sch.NextRunAt.IsZero() || sch.NextRunAt.After(now) {
			continue
		}

		if overdue := now.Sub(sch.NextRunAt); overdue > runnerCatchUpWindow {
			r.reanchor(sch, now, tz)
			continue
		}

		missing := r.missingConnectors(sch)
		if len(missing) == 0 && r.gw.IsBusy() {
			// Defer, don't interrupt: NextRunAt stays so it is retried next tick.
			continue
		}

		r.fire(sch, tz, missing)
	}
}

// missingConnectors returns uninstalled codes from sch.Requires in order, ignoring blanks and repeats.
func (r *Runner) missingConnectors(sch Schedule) []string {
	if r.connectors == nil || len(sch.Requires) == 0 {
		return nil
	}
	var missing []string
	seen := make(map[string]bool, len(sch.Requires))
	for _, code := range sch.Requires {
		code = strings.TrimSpace(code)
		if code == "" || seen[code] {
			continue
		}
		seen[code] = true
		if !r.connectors.Installed(code) {
			missing = append(missing, code)
		}
	}
	return missing
}

// attempt returns a skip report when connectors are missing, otherwise the gateway send result.
func (r *Runner) attempt(sch Schedule, missing []string, attemptID string) RunReport {
	if len(missing) > 0 {
		return r.skip(sch, missing)
	}
	return r.send(sch, attemptID)
}

// skip builds the report for a run skipped over missing connectors; it does not persist or report.
func (r *Runner) skip(sch Schedule, missing []string) RunReport {
	slog.Info("schedule: skipped, required connector not installed", "component", "schedule",
		"schedule_id", sch.ID, "missing", missing)
	return RunReport{
		ScheduleID: sch.ID,
		Name:       sch.Name,
		StartedAt:  time.Now(),
		Status:     RunStatusSkipped,
		Summary:    missingConnectorSummaryPrefix + strings.Join(missing, ", "),
	}
}

// reanchor moves a too-stale schedule to its next occurrence from now without recording a run.
func (r *Runner) reanchor(sch Schedule, now time.Time, tz *time.Location) {
	next, ok := sch.NextRun(now, tz, r.deviceID)
	if !ok {
		next = time.Time{} // spent (a "once"/end_at case) — leave it un-due
	}
	if err := r.store.SetNextRun(sch.ID, next); err != nil {
		slog.Error("schedule: reanchor failed", "component", "schedule", "schedule_id", sch.ID, "error", err)
	}
}

// fire runs one due occurrence and persists/acks the outcome. A failure keeps NextRunAt
// (retried within the catch-up window) and acks once per occurrence; success and skip advance
// from the de-jittered anchor (DejitterAnchor, else the same occurrence repeats) and always ack.
func (r *Runner) fire(sch Schedule, tz *time.Location, missing []string) {
	attemptID := fmt.Sprintf("sched-%s-%d", sch.ID, time.Now().UnixMilli())
	rr := r.attempt(sch, missing, attemptID)

	if rr.Status == "failure" {
		alreadyAckedThisOccurrence := sch.LastRunStatus == "failure" && sch.LastFailedOccurrence.Equal(sch.NextRunAt)

		// I5: never advance NextRunAt on failure, so the occurrence is retried next tick.
		if err := r.store.SetLastFailedRun(sch.ID, rr.StartedAt, rr.Summary, sch.NextRunAt); err != nil {
			slog.Error("schedule: persist failed run failed", "component", "schedule", "schedule_id", sch.ID, "error", err)
		}
		if !alreadyAckedThisOccurrence && r.report != nil {
			r.report(rr)
		}
		return
	}

	base := DejitterAnchor(sch.Cadence.Repeat, sch.NextRunAt, r.deviceID, sch.ID)
	next, ok := sch.NextRun(base, tz, r.deviceID)
	if !ok {
		next = time.Time{}
	}
	if err := r.store.RecordRunResult(sch.ID, rr.StartedAt, rr.Status, rr.Summary, next); err != nil {
		slog.Error("schedule: persist run result failed", "component", "schedule", "schedule_id", sch.ID, "error", err)
	}
	// Carry the next occurrence on the ack; the backend has no other source.
	rr.NextRunAt = next
	if r.report != nil {
		r.report(rr)
	}
}

// RunNow fires sch immediately, updating only last-run fields (NextRunAt untouched).
// ok=false means deferred because the agent is busy; skips bypass the busy check.
func (r *Runner) RunNow(sch Schedule) (report RunReport, ok bool) {
	missing := r.missingConnectors(sch)
	if len(missing) == 0 && r.gw.IsBusy() {
		return RunReport{}, false
	}
	attemptID := fmt.Sprintf("sched-run-%s-%d", sch.ID, time.Now().UnixMilli())
	rr := r.attempt(sch, missing, attemptID)
	rr.Manual = true
	if err := r.store.SetLastRun(sch.ID, rr.StartedAt, rr.Status, rr.Summary); err != nil {
		slog.Error("schedule: persist manual run failed", "component", "schedule", "schedule_id", sch.ID, "error", err)
	}
	if r.report != nil {
		r.report(rr)
	}
	return rr, true
}

// send delivers sch via Speak (KindSpeak) or SendSystemChatMessage; it neither persists nor reports.
// attemptID is a log-only correlation id, not RunReport.RunID.
func (r *Runner) send(sch Schedule, attemptID string) RunReport {
	started := time.Now()
	kind := ResolveKind(sch.Kind)
	slog.Info("schedule: firing", "component", "schedule",
		"schedule_id", sch.ID, "kind", kind, "attempt_id", attemptID)

	var (
		runID string
		err   error
	)
	if kind == KindSpeak {
		err = r.gw.Speak(sch.Instructions)
	} else {
		// The [via:schedule] line marks this as a schedule run: the instructions
		// are plain prompt text that otherwise reads like a typed request.
		runID, err = r.gw.SendSystemChatMessage(domain.AppendVia(sch.Instructions, domain.ViaSchedule))
	}
	latency := time.Since(started)

	status, summary := "success", sch.Name
	if err != nil {
		status, summary = "failure", err.Error()
		slog.Error("schedule: fire failed", "component", "schedule",
			"schedule_id", sch.ID, "kind", kind, "error", err)
	}

	return RunReport{ScheduleID: sch.ID, Name: sch.Name, RunID: runID, StartedAt: started, SendLatency: latency, Status: status, Summary: summary}
}
