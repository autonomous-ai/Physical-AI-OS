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
	// runnerTickInterval mirrors the connector-refresh loop's cadence class:
	// frequent enough that a schedule fires within a minute of its due time,
	// cheap because the common case (nothing due) is a fast no-op scan.
	runnerTickInterval = 1 * time.Minute

	// runnerCatchUpWindow bounds how overdue a schedule may be before the
	// runner gives up firing it and just re-anchors it forward instead. A
	// device that was powered off overnight must not fire every missed daily
	// briefing back-to-back the moment it boots — only a run that was due
	// within this window still fires (once); anything staler is silently
	// skipped forward to its next regular occurrence.
	runnerCatchUpWindow = 30 * time.Minute
)

const (
	// RunStatusSkipped is the RunReport.Status (and Schedule.LastRunStatus) of
	// an occurrence the device deliberately did not run because a connector in
	// Schedule.Requires is not installed. Exact string per the schedule.run
	// wire contract ("success" | "failure" | "skipped") — the backend and the
	// web app match it verbatim.
	RunStatusSkipped = "skipped"

	// missingConnectorSummaryPrefix starts a skipped run's Summary, followed
	// by the missing codes in Requires order joined by ", " — e.g.
	// "missing connector: gmail, slack". The format is part of the wire
	// contract: the web parses the codes back out to render labels.
	missingConnectorSummaryPrefix = "missing connector: "
)

// RunReport is handed to the report callback after every fire attempt
// (success, failure or skip), whether it came from the ticker or from a manual
// "Run now". The MQTT layer (schedule_sync_handler.go / schedule_run_handler.go)
// turns this into the fd_channel schedule.run ack; the Runner itself knows
// nothing about MQTT.
type RunReport struct {
	ScheduleID string
	// Name is the schedule's name at the moment it ran, in EVERY outcome.
	// Summary cannot stand in for it: Summary is the name only on success —
	// it is the error text on a failure and the missing connectors on a skip —
	// and everything downstream of the report callback (the ops alert in
	// particular: "❌ Schedule run failed: <name>") must still say WHICH task
	// it was. Carried here rather than re-read from the store by the callback,
	// so it is exactly the name that ran, with no extra disk read on the tick.
	Name string
	// Manual is true when the run came from RunNow (the "Run now" button, via
	// MQTT schedule.run or the local HTTP endpoint) and false for the ticker's
	// own fires. The report callback is shared by both paths and has no other
	// way to tell them apart; the ops alert uses it for its "(run now)" suffix.
	// Not on the schedule.run wire — the ack is identical either way.
	Manual bool
	// RunID is the run id domain.AgentGateway.SendSystemChatMessage itself
	// returned — empty on a failed send, since no run ever started. This is
	// deliberately NOT a locally fabricated id: SendSystemChatMessage is
	// fire-and-forget and its return value IS the id Flow Monitor/the backend
	// can actually correlate against (domain/agent.go's doc comment). An
	// earlier revision fabricated its own id here and threw the real one away
	// into Summary instead — CRITICAL-3 from the phase-5 review.
	//
	// ALWAYS EMPTY for a KindSpeak schedule, success included: speaking runs no
	// agent turn, so there is no run to correlate with and nothing honest to
	// put here. Likewise empty for a skipped run, which never reached the
	// gateway at all. Consumers must not read an empty RunID as failure —
	// read Status.
	RunID     string
	StartedAt time.Time
	// SendLatency is how long the call to hand the message to the runtime
	// took — NOT how long the agent's turn took. SendSystemChatMessage returns
	// as soon as the message is sent (fire-and-forget), so this is send
	// latency, typically single-digit milliseconds, never turn duration.
	//
	// For a KindSpeak schedule it is the time HAL took to ACCEPT the text, not
	// how long the resulting speech lasted — /voice/speak returns on accept.
	SendLatency time.Duration

	// Status is "success" | "failure" | "skipped". What a "success" certifies
	// differs by kind — in BOTH cases it is weaker than "the user got their
	// task":
	//
	//   KindAgent — the runtime accepted the message and returned a run id. The
	//     turn itself runs asynchronously afterwards, so a "success" says
	//     nothing about whether the agent answered well, or at all.
	//   KindSpeak — HAL accepted the text for playback. hal.Speak returns on
	//     accept and reports only a transport-level outcome: it cannot tell us
	//     that audio actually played, that a speaker was attached, that the
	//     volume was above zero, or that anybody was in the room. "success"
	//     here means WE SAID IT, not THEY HEARD IT, and no code (or UI copy)
	//     should claim otherwise.
	//
	// A "failure" is meaningful in both cases: the device genuinely could not
	// hand the task off, and it will be retried within the catch-up window.
	//
	// "skipped" (RunStatusSkipped) means the device deliberately did NOT run
	// the task because a connector in Schedule.Requires is not installed. It
	// is not a failure: nothing is retried, nothing may treat it as one (the
	// ops alert reports it as a skip, never as a failed run), and the
	// occurrence is consumed exactly like a success (NextRunAt advances).
	// The gateway is never called, for either kind.
	Status string
	// Summary is the schedule's Name on success; the error text on failure;
	// "missing connector: <code>[, <code>…]" when skipped (the missing codes
	// in Requires order — see missingConnectorSummaryPrefix).
	Summary string
	// NextRunAt is the freshly computed next occurrence, set by fire() right
	// after it persists the same value via RecordRunResult — on a success or
	// a skip (zero value otherwise: a failed fire never advances NextRunAt,
	// see fire()'s I5 comment, and RunNow deliberately never touches it
	// either).
	//
	// CRITICAL FIX (final review): before this field existed, the schedule.run
	// ack never carried the device's newly computed next-fire time at all —
	// the only other writer of next_run_at is a schedule.sync ack, which only
	// happens on a user edit or while a row is still "pending". So a schedule
	// showed "in 16 hours" until its first fire, then "now" PERMANENTLY for
	// the rest of its life (the web UI's formatCadence.ts treats any
	// non-positive diff as "now") — the feature's headline column, broken
	// forever after the very first run.
	NextRunAt time.Time
}

// Runner is the on-device scheduler loop: once a minute it asks the Store
// which schedules are due and fires them through domain.AgentGateway —
// SendSystemChatMessage for an "agent" task, Speak for a "speak" one. Both are
// methods all six agentic runtimes implement, which is why the scheduler lives
// here instead of inside any one of them, and why it never imports the hal
// package directly: depending on the interface alone is what keeps this whole
// loop testable against a fake gateway.
type Runner struct {
	store    *Store
	gw       domain.AgentGateway
	deviceID string
	report   func(RunReport)

	// connectors answers "is connector <code> installed on this device?" for
	// the Schedule.Requires guard. nil means NO guard: every requirement
	// counts as met and every task runs exactly as it did before the field
	// existed — the zero configuration every existing NewRunner caller (and
	// test) gets. See SetConnectorChecker.
	connectors ConnectorChecker
}

// ConnectorChecker reports whether a connector code (a catalog code, exactly
// as in connector.set.<code>) currently has credentials on this device.
//
// Defined here, next to its one consumer, rather than in the MQTT package that
// implements it: the Runner must stay testable against a fake, and it has no
// business knowing WHERE connector credentials live on disk (that is the
// connector writers' concern — see mqtthandler's connectorInstalled).
//
// Implementations should answer false ONLY on positive evidence that the
// connector is absent. When the answer is genuinely unknown (a store that
// could not be read), true is the safe answer: it degrades to running the
// task the way it ran before this guard existed, whereas a wrong false tells
// the user to reconnect something that may be connected and silently drops
// their task.
type ConnectorChecker interface {
	Installed(code string) bool
}

// ConnectorCheckerFunc adapts a plain function to ConnectorChecker, the way
// http.HandlerFunc adapts one to http.Handler.
type ConnectorCheckerFunc func(code string) bool

// Installed calls f(code).
func (f ConnectorCheckerFunc) Installed(code string) bool { return f(code) }

// SetConnectorChecker enables the Schedule.Requires guard. A setter rather
// than a NewRunner parameter so the zero configuration stays "no guard" for
// every caller that has no notion of connectors. Call it once, right after
// NewRunner and before Start — it is not synchronized against a running tick.
func (r *Runner) SetConnectorChecker(c ConnectorChecker) { r.connectors = c }

// NewRunner builds a Runner. deviceID is threaded in explicitly (rather than
// read from config inside this package) purely so NextRun's jitter has
// somewhere to get it from without this package reaching into global state —
// see JitterOffset. report may be nil (tests that don't care about acks).
func NewRunner(store *Store, gw domain.AgentGateway, deviceID string, report func(RunReport)) *Runner {
	return &Runner{store: store, gw: gw, deviceID: deviceID, report: report}
}

// Start runs the ticker loop until ctx is cancelled. The first tick fires
// immediately (boot catch-up: schedules that came due while the device was
// off get evaluated right away instead of waiting up to a full tick
// interval), then it settles into the regular cadence.
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

// safeTick recovers from a panic in a single tick so one bad schedule (a nil
// pointer somewhere, a malformed Cadence that slipped past validation, ...)
// can never kill the whole loop — mirrors StartConnectorRefreshLoop's
// per-tick recovery.
func (r *Runner) safeTick(now time.Time) {
	defer func() {
		if rec := recover(); rec != nil {
			slog.Error("schedule: panic in runner tick", "component", "schedule", "panic", rec)
		}
	}()
	r.tick(now)
}

// tick evaluates every schedule once. Due schedules are fired one at a time,
// in order — never concurrently — and IsBusy() is checked immediately before
// each one, so if firing the first schedule puts the agent into a turn, a
// second schedule due in the very same tick is deferred rather than firing on
// top of it. It stays due and gets its turn on a later tick once the agent
// frees up (see the SingleFlight test).
//
// EXCEPT a skip: the connector guard (Schedule.Requires) is evaluated BEFORE
// the busy check, and a task that is going to be skipped fires regardless of
// IsBusy(). Single-flight exists to keep two agent turns (or two voices)
// apart, and a skip starts neither — it never touches the gateway. Deferring
// it would buy nothing and could cost the report entirely: a skip deferred
// behind a turn longer than the catch-up window would be re-anchored forward
// silently, and the user would never learn why their task did not run.
func (r *Runner) tick(now time.Time) {
	schedules, err := r.store.Load()
	if err != nil {
		slog.Error("schedule: load failed", "component", "schedule", "error", err)
		return
	}
	tz := r.store.Timezone()

	for _, sch := range schedules {
		if !sch.Enabled || sch.NextRunAt.IsZero() || sch.NextRunAt.After(now) {
			continue // disabled, never computed, or simply not due yet
		}

		if overdue := now.Sub(sch.NextRunAt); overdue > runnerCatchUpWindow {
			r.reanchor(sch, now, tz)
			continue
		}

		missing := r.missingConnectors(sch)
		if len(missing) == 0 && r.gw.IsBusy() {
			// Defer, don't interrupt: NextRunAt is left untouched so this same
			// schedule is re-evaluated (and, catch-up window permitting, fired)
			// on the next tick. Only a task that will actually reach the
			// gateway waits — see the doc comment on skips.
			continue
		}

		r.fire(sch, tz, missing)
	}
}

// missingConnectors returns the codes in sch.Requires that are not installed
// on this device, in Requires order — the order the skip summary reports
// them in. Empty means "run it": no requirements, every requirement met, or no
// checker configured (nil checker = no guard, see Runner.connectors).
//
// Blank entries and repeats are ignored rather than trusted: the backend
// dedupes before sending, but a malformed list must not turn into a skip
// summary like "missing connector: , gmail, gmail", nor ask the checker the
// same question twice.
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

// attempt produces the outcome of one fire attempt: a skip report when a
// required connector is missing (the gateway is NOT called), otherwise the
// real gateway send. Shared by fire() and RunNow() so the guard can never be
// applied on one path and forgotten on the other.
func (r *Runner) attempt(sch Schedule, missing []string, attemptID string) RunReport {
	if len(missing) > 0 {
		return r.skip(sch, missing)
	}
	return r.send(sch, attemptID)
}

// skip builds the report for a run the device deliberately did not perform.
// Like send(), it neither persists nor reports — its callers decide that.
//
// Logged at Info, not Warn/Error: this is the guard working as designed on
// a device whose owner has not connected a service, and a daily task would
// otherwise put a warning in the log every single day until they do.
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

// reanchor skips a too-stale-to-fire schedule forward to its next regular
// occurrence from `now`, without recording a run — it never actually ran, so
// LastRunAt/LastRunStatus are untouched.
func (r *Runner) reanchor(sch Schedule, now time.Time, tz *time.Location) {
	next, ok := sch.NextRun(now, tz, r.deviceID)
	if !ok {
		next = time.Time{} // spent (a "once"/end_at case) — leave it un-due
	}
	if err := r.store.SetNextRun(sch.ID, next); err != nil {
		slog.Error("schedule: reanchor failed", "component", "schedule", "schedule_id", sch.ID, "error", err)
	}
}

// fire sends sch's instructions through the gateway. On success it persists
// both the run outcome and the freshly computed next occurrence in one atomic
// store write; on failure it records the outcome WITHOUT touching NextRunAt
// (see the I5 comment below).
//
// The next occurrence is anchored on sch.NextRunAt (the due time just fired),
// not on `now` — using `now` would compress or stretch the cadence gap by
// however late this particular tick happened to run. But sch.NextRunAt is
// itself a JITTERED value (see JitterOffset), and NextRun expects an
// unjittered anchor — feeding the jittered value straight back in can return
// the SAME occurrence again, so the schedule never advances (CRITICAL-2 from
// the phase-5 review). DejitterAnchor reverses that shift first.
//
// Ack suppression (phase-5 review, round 2): I5's retry means a persistently
// failing send (agent WebSocket down, MQTT fd_channel up — the two are
// independent transports, so this is a real, reachable state, not a
// theoretical one) gets attempted on every tick for up to the 30-minute
// catch-up window — roughly 31 attempts for ONE missed occurrence. The RETRY
// is correct and must keep happening every tick. But each retry used to also
// invoke the report callback, and the backend writes one schedule_run history
// row PER ack — so one missed briefing became "failed 31 times". A failure is
// now ack'd only the FIRST time for a given occurrence (tracked via
// LastFailedOccurrence — see its doc comment); a SUCCESS always acks,
// including a success that follows earlier suppressed failures in the same
// occurrence, since that is the outcome the backend actually needs to hear.
//
// A SKIP (missing is non-empty: a connector in Schedule.Requires is not
// installed — see attempt()) takes the SUCCESS path, not the failure one, on
// purpose: the occurrence was deliberately resolved, not lost. Retrying it
// every tick would change nothing (connecting a service goes through the
// backend, not through this loop) and would only re-skip, so NextRunAt
// advances and the skip is always acked — once per occurrence, since the
// advance itself is what stops a second attempt. The branch below therefore
// tests for "failure" explicitly rather than "anything but success": the
// latter would have routed a skip into I5's retry and its ack suppression,
// which could swallow the skip entirely when a failure of the same
// occurrence had already been acked.
func (r *Runner) fire(sch Schedule, tz *time.Location, missing []string) {
	attemptID := fmt.Sprintf("sched-%s-%d", sch.ID, time.Now().UnixMilli())
	rr := r.attempt(sch, missing, attemptID)

	if rr.Status == "failure" {
		alreadyAckedThisOccurrence := sch.LastRunStatus == "failure" && sch.LastFailedOccurrence.Equal(sch.NextRunAt)

		// I5: a send failure must NOT burn the occurrence by advancing
		// NextRunAt. Leaving it untouched keeps the schedule "due" so it is
		// retried on the next tick(s) — bounded by the same 30-minute
		// catch-up window tick() already enforces (past that, tick()
		// re-anchors forward instead of calling fire() at all). Advancing
		// NextRunAt here would mean e.g. a WS reconnect exactly at 08:00
		// permanently loses that day's run instead of retrying moments later.
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
	// Forward the same occurrence onto the ack (see RunReport.NextRunAt's doc
	// comment) — the backend has no other reliable way to learn it.
	rr.NextRunAt = next
	if r.report != nil {
		r.report(rr) // always ack a success or skip, even after earlier suppressed failures this occurrence
	}
}

// RunNow fires sch immediately for the "Run now" button (kind schedule.run).
// Unlike the ticker's fire(), it updates ONLY last-run bookkeeping — cadence
// and NextRunAt are left exactly as they were, matching the wire contract's
// note that running now must not perturb the schedule's regular cadence.
//
// ok=false means the run was deferred (the agent is busy) rather than
// attempted at all — the same single-flight rule the ticker follows applies
// here too, so a manual trigger can never barge in on an active turn.
//
// The connector guard (Schedule.Requires) applies here exactly as it does on
// the ticker, and is likewise checked BEFORE the busy check: a skip never
// touches the gateway, so it answers straight away with the real reason
// ("missing connector: gmail") instead of "agent busy, try again", which
// would only lead to the same skip on the retry. A skip is a completed run
// (ok=true), recorded via SetLastRun like any other manual outcome.
func (r *Runner) RunNow(sch Schedule) (report RunReport, ok bool) {
	missing := r.missingConnectors(sch)
	if len(missing) == 0 && r.gw.IsBusy() {
		return RunReport{}, false
	}
	attemptID := fmt.Sprintf("sched-run-%s-%d", sch.ID, time.Now().UnixMilli())
	rr := r.attempt(sch, missing, attemptID)
	rr.Manual = true // before the callback sees it — see RunReport.Manual
	if err := r.store.SetLastRun(sch.ID, rr.StartedAt, rr.Status, rr.Summary); err != nil {
		slog.Error("schedule: persist manual run failed", "component", "schedule", "schedule_id", sch.ID, "error", err)
	}
	// A manual "Run now" always acks — there is no retry/suppression concept
	// here (unlike fire()'s automatic ticker path): one request, one ack.
	if r.report != nil {
		r.report(rr)
	}
	return rr, true
}

// send performs the actual gateway call and classifies the outcome. It does
// NOT touch the store and does NOT invoke the report callback — fire() and
// RunNow() decide separately what to persist and whether/when to report,
// since the ticker's automatic retries must suppress repeated failure acks
// for the same occurrence (see fire()'s doc comment) while RunNow always acks.
//
// THIS IS THE ONE PLACE THE TWO KINDS DIVERGE. Everything around it — when a
// task is due, the catch-up window, single-flight, I5's retry, failure-ack
// suppression, the connector guard (attempt() never even calls this for a
// skip), next-occurrence math, the ack payload — is deliberately shared,
// because none of it depends on what firing actually does. A "speak" task is a
// scheduled task in every respect except which gateway method delivers it.
//
//	KindAgent — SendSystemChatMessage. Fire-and-forget; its return value is the
//	  run id the backend can correlate against.
//	KindSpeak — Speak. The instructions ARE the words, posted straight to TTS.
//	  No agent turn runs, so there is no run id (RunReport.RunID stays empty by
//	  design) and no tokens are spent.
//
// Unknown/empty kinds route to the agent path via ResolveKind — see its doc
// comment for why degrading is right and rejecting is not.
//
// Note what is NOT special-cased: tick() still refuses to fire ANY kind while
// the gateway reports IsBusy(). A speak task runs no agent turn, so it is
// tempting to let it barge in — but the resource it contends for is the
// SPEAKER (and the mic, which HAL locks during playback), not the model. Two
// voices at once is exactly what the single-flight rule exists to prevent, so
// speak waits its turn like everything else.
//
// attemptID is a LOCAL correlation id used only for the "firing" log line
// before the send completes — it is NOT reported as RunReport.RunID. For an
// agent task the actual run id comes from SendSystemChatMessage's own return
// value once the send completes (see RunReport.RunID's doc comment).
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
		// No runID: nothing started that anyone could look up later.
		err = r.gw.Speak(sch.Instructions)
	} else {
		runID, err = r.gw.SendSystemChatMessage(sch.Instructions)
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
