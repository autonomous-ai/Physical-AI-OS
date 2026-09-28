package schedule

import (
	"encoding/json"
	"errors"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fakeConnectors is the test double for ConnectorChecker: a fixed set of
// installed connector codes, plus a record of every code the runner asked
// about so a test can prove the guard was (or was not) consulted at all.
type fakeConnectors struct {
	installed map[string]bool
	asked     []string
}

func (f *fakeConnectors) Installed(code string) bool {
	f.asked = append(f.asked, code)
	return f.installed[code]
}

func installed(codes ...string) *fakeConnectors {
	m := make(map[string]bool, len(codes))
	for _, c := range codes {
		m[c] = true
	}
	return &fakeConnectors{installed: m}
}

// dailyWithRequires is a due-able daily schedule carrying the given
// requirement list — the shape a template-created task arrives in.
func dailyWithRequires(requires ...string) Schedule {
	return Schedule{
		ID: "s1", Name: "Inbox digest", Instructions: "Summarize my unread email", Enabled: true,
		Cadence:  Spec{Repeat: RepeatDaily, Time: "08:00"},
		Requires: requires,
	}
}

// TestScheduleRequiresParsesFromSyncWire pins the wire key: the backend sends
// the connector codes a template task needs as "requires" on each
// schedule.sync element, and they must land on the stored Schedule in the
// order sent (the skip summary reports them in that order).
func TestScheduleRequiresParsesFromSyncWire(t *testing.T) {
	const wire = `{
		"id": "s1",
		"name": "Inbox digest",
		"instructions": "Summarize my unread email",
		"enabled": true,
		"schedule": {"repeat": "daily", "time": "08:00"},
		"requires": ["gmail", "slack"],
		"end_at": null,
		"rev": 2
	}`
	var got Schedule
	if err := json.Unmarshal([]byte(wire), &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if strings.Join(got.Requires, ",") != "gmail,slack" {
		t.Fatalf("Requires = %v, want [gmail slack] in wire order", got.Requires)
	}
}

// TestScheduleWithoutRequiresOnWireHasNone is the compatibility half: every
// row that predates the field (and every custom task) arrives without the key
// and must carry no requirement at all.
func TestScheduleWithoutRequiresOnWireHasNone(t *testing.T) {
	const wire = `{"id": "s1", "name": "x", "instructions": "y", "enabled": true,
		"schedule": {"repeat": "daily", "time": "08:00"}}`
	var got Schedule
	if err := json.Unmarshal([]byte(wire), &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(got.Requires) != 0 {
		t.Fatalf("Requires = %v, want none", got.Requires)
	}
}

// TestScheduleRequiresSurvivesStoreRoundTripAndIsOmittedWhenEmpty checks the
// field persists across the atomic write + reload, AND that a row without
// requirements gains no "requires" key on disk — upgrading a device must not
// rewrite every stored row.
func TestScheduleRequiresSurvivesStoreRoundTripAndIsOmittedWhenEmpty(t *testing.T) {
	path := filepath.Join(t.TempDir(), "schedules.json")
	store := NewStore(path)

	withReq := dailyWithRequires("gmail")
	plain := Schedule{ID: "s2", Name: "Stretch", Instructions: "Stand up", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "09:00"}}
	if err := store.Replace([]Schedule{withReq, plain}); err != nil {
		t.Fatalf("Replace: %v", err)
	}

	got, _ := store.Get("s1")
	if strings.Join(got.Requires, ",") != "gmail" {
		t.Fatalf("reloaded Requires = %v, want [gmail]", got.Requires)
	}
	raw, err := readFile(path)
	if err != nil {
		t.Fatalf("read schedules.json: %v", err)
	}
	if strings.Count(raw, `"requires"`) != 1 {
		t.Fatalf("expected exactly one \"requires\" key on disk (the template row), got:\n%s", raw)
	}
}

// TestReplace_RequiresFollowsTheWireNotThePriorRow: requires is backend-owned
// wire data, not device-local bookkeeping, so a full-state schedule.sync that
// no longer lists a requirement must clear it rather than have the old list
// carried forward (the way LastRunAt is).
func TestReplace_RequiresFollowsTheWireNotThePriorRow(t *testing.T) {
	store := newTestStore(t)
	if err := store.Replace([]Schedule{dailyWithRequires("gmail")}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	if err := store.Replace([]Schedule{dailyWithRequires()}); err != nil {
		t.Fatalf("Replace: %v", err)
	}
	got, _ := store.Get("s1")
	if len(got.Requires) != 0 {
		t.Fatalf("Requires = %v after a sync without it, want cleared", got.Requires)
	}
}

// The headline behaviour: a due task whose required connector is not
// installed never reaches the gateway, is reported as "skipped" with the
// exact summary format, and — unlike a failure — advances NextRunAt, with the
// fresh occurrence carried on the report.
func TestRunner_SkipsWhenRequiredConnectorMissing(t *testing.T) {
	store := newTestStore(t)
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", dailyWithRequires("gmail"))
	now := scheduledAt.Add(time.Minute)

	gw := &fakeGateway{}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.SetConnectorChecker(installed()) // nothing installed
	r.tick(now)

	if len(gw.sent) != 0 || len(gw.spoken) != 0 {
		t.Fatalf("gateway was called for a task with a missing connector: sent=%v spoken=%v", gw.sent, gw.spoken)
	}
	if len(reports) != 1 {
		t.Fatalf("reports = %+v, want exactly 1", reports)
	}
	rr := reports[0]
	if rr.Status != "skipped" {
		t.Errorf("Status = %q, want skipped", rr.Status)
	}
	if rr.Summary != "missing connector: gmail" {
		t.Errorf("Summary = %q, want %q", rr.Summary, "missing connector: gmail")
	}
	if rr.ScheduleID != "s1" || rr.RunID != "" || rr.StartedAt.IsZero() {
		t.Errorf("report = %+v, want id s1, empty run id, a start time", rr)
	}

	got, _ := store.Get("s1")
	if !got.NextRunAt.After(now) {
		t.Errorf("NextRunAt not advanced past the skipped occurrence: %v", got.NextRunAt)
	}
	if !rr.NextRunAt.Equal(got.NextRunAt) {
		t.Errorf("report NextRunAt = %v, want the persisted %v", rr.NextRunAt, got.NextRunAt)
	}
	if got.LastRunStatus != "skipped" {
		t.Errorf("LastRunStatus = %q, want skipped", got.LastRunStatus)
	}

	// Advanced, not retried: the next tick inside the old catch-up window
	// must neither fire nor report again.
	r.tick(now.Add(time.Minute))
	if len(reports) != 1 || len(gw.sent) != 0 {
		t.Fatalf("skipped occurrence was retried: reports=%d sent=%v", len(reports), gw.sent)
	}
}

func TestRunner_RunsWhenAllRequiredConnectorsInstalled(t *testing.T) {
	store := newTestStore(t)
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", dailyWithRequires("gmail", "slack"))

	gw := &fakeGateway{}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.SetConnectorChecker(installed("gmail", "slack"))
	r.tick(scheduledAt.Add(time.Minute))

	if len(gw.sent) != 1 {
		t.Fatalf("sent = %v, want the task to run when every requirement is installed", gw.sent)
	}
	if len(reports) != 1 || reports[0].Status != "success" {
		t.Fatalf("reports = %+v, want one success", reports)
	}
}

// Custom tasks and every pre-existing row carry no requirement: they run as
// today even when nothing at all is installed, and the checker is never asked.
func TestRunner_EmptyRequiresRunsNormally(t *testing.T) {
	store := newTestStore(t)
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", dailyWithRequires())

	gw := &fakeGateway{}
	checker := installed()
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.SetConnectorChecker(checker)
	r.tick(scheduledAt.Add(time.Minute))

	if len(gw.sent) != 1 || len(reports) != 1 || reports[0].Status != "success" {
		t.Fatalf("sent=%v reports=%+v, want a normal successful run", gw.sent, reports)
	}
	if len(checker.asked) != 0 {
		t.Errorf("checker consulted for a task with no requirements: %v", checker.asked)
	}
}

// A nil checker means no guard at all — the Runner's zero configuration keeps
// today's behaviour even for a task that lists requirements.
func TestRunner_NilCheckerDisablesGuard(t *testing.T) {
	store := newTestStore(t)
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", dailyWithRequires("gmail"))

	gw := &fakeGateway{}
	r := NewRunner(store, gw, "device-1", nil)
	r.tick(scheduledAt.Add(time.Minute))

	if len(gw.sent) != 1 {
		t.Fatalf("sent = %v, want the task to run with no checker configured", gw.sent)
	}
}

// Summary lists ONLY the missing codes, in requires order, joined by ", ".
// Blank entries and repeats in the wire list are ignored rather than
// rendered as "missing connector: , gmail, gmail".
func TestRunner_SkipSummaryListsMissingCodesInRequiresOrder(t *testing.T) {
	store := newTestStore(t)
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1",
		dailyWithRequires("slack", "gmail", " ", "notion", "slack"))

	gw := &fakeGateway{}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.SetConnectorChecker(installed("gmail"))
	r.tick(scheduledAt.Add(time.Minute))

	if len(reports) != 1 {
		t.Fatalf("reports = %+v", reports)
	}
	if want := "missing connector: slack, notion"; reports[0].Summary != want {
		t.Fatalf("Summary = %q, want %q", reports[0].Summary, want)
	}
}

// The failure-ack suppression (one failure ack per occurrence) must not
// swallow a skip: an occurrence whose failure was already acked, and whose
// connector then disappears before the retry, still reports the skip.
func TestRunner_SkipIsReportedEvenAfterAFailureAckInTheSameOccurrence(t *testing.T) {
	store := newTestStore(t)
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", dailyWithRequires("gmail"))

	gw := &fakeGateway{sendErr: errors.New("ws disconnected")}
	checker := installed("gmail")
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.SetConnectorChecker(checker)

	r.tick(scheduledAt) // send fails -> the occurrence's one failure ack
	if len(reports) != 1 || reports[0].Status != "failure" {
		t.Fatalf("reports = %+v, want one failure ack", reports)
	}

	delete(checker.installed, "gmail") // user disconnects Gmail before the retry
	r.tick(scheduledAt.Add(time.Minute))
	if len(reports) != 2 || reports[1].Status != "skipped" {
		t.Fatalf("reports = %+v, want the skip reported after the suppressed-failure state", reports)
	}
	got, _ := store.Get("s1")
	if !got.NextRunAt.After(scheduledAt) {
		t.Errorf("NextRunAt = %v, want advanced past the skipped occurrence", got.NextRunAt)
	}
}

// Every skipped occurrence reports once — two consecutive days skipped are
// two acks, not one suppressed into the other.
func TestRunner_EachSkippedOccurrenceReportsOnce(t *testing.T) {
	store := newTestStore(t)
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", dailyWithRequires("gmail"))

	gw := &fakeGateway{}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.SetConnectorChecker(installed())

	r.tick(scheduledAt)
	next, _ := store.Get("s1")
	r.tick(next.NextRunAt)

	if len(reports) != 2 || reports[0].Status != "skipped" || reports[1].Status != "skipped" {
		t.Fatalf("reports = %+v, want one skipped ack per occurrence", reports)
	}
}

// A skip never touches the gateway, so the single-flight rule (which exists
// to keep two turns/voices apart) has nothing to protect: a skip due while the
// agent is busy is recorded at its due time instead of being deferred — and
// possibly aged past the catch-up window without ever being reported.
func TestRunner_SkipIsRecordedEvenWhileAgentBusy(t *testing.T) {
	store := newTestStore(t)
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", dailyWithRequires("gmail"))

	gw := &fakeGateway{busy: true}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.SetConnectorChecker(installed())
	r.tick(scheduledAt.Add(time.Minute))

	if len(gw.sent) != 0 {
		t.Fatalf("sent = %v while busy", gw.sent)
	}
	if len(reports) != 1 || reports[0].Status != "skipped" {
		t.Fatalf("reports = %+v, want the skip recorded despite the busy agent", reports)
	}
}

// "Run now" on a task whose connector is missing: no gateway call, a skipped
// report, LastRunStatus recorded — and NextRunAt untouched, exactly as for a
// normal manual run.
func TestRunNow_SkipsWhenRequiredConnectorMissing(t *testing.T) {
	store := newTestStore(t)
	scheduledAt := time.Date(2026, 8, 27, 8, 0, 0, 0, time.UTC)
	sch := dailyWithRequires("gmail", "google_calendar")
	sch.NextRunAt = scheduledAt
	if err := store.Replace([]Schedule{sch}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	sch, _ = store.Get("s1")

	gw := &fakeGateway{}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.SetConnectorChecker(installed("gmail"))

	rr, ok := r.RunNow(sch)
	if !ok {
		t.Fatal("RunNow returned ok=false; a skip is a completed run, not a deferral")
	}
	if len(gw.sent) != 0 {
		t.Fatalf("sent = %v, want no gateway call", gw.sent)
	}
	if rr.Status != "skipped" || rr.Summary != "missing connector: google_calendar" {
		t.Fatalf("report = %+v, want skipped / missing connector: google_calendar", rr)
	}
	if len(reports) != 1 || reports[0].Status != "skipped" {
		t.Fatalf("callback reports = %+v, want exactly one skipped ack", reports)
	}
	if !rr.NextRunAt.IsZero() {
		t.Errorf("RunNow report NextRunAt = %v, want zero (manual runs never advance it)", rr.NextRunAt)
	}

	got, _ := store.Get("s1")
	if got.LastRunStatus != "skipped" {
		t.Errorf("LastRunStatus = %q, want skipped", got.LastRunStatus)
	}
	if !got.NextRunAt.Equal(scheduledAt) {
		t.Errorf("RunNow changed NextRunAt: got %v, want untouched %v", got.NextRunAt, scheduledAt)
	}
}

// Same reasoning as the ticker: a manual skip needs no agent, so it answers
// immediately instead of "agent busy, try again".
func TestRunNow_SkipAnswersEvenWhileAgentBusy(t *testing.T) {
	store := newTestStore(t)
	if err := store.Replace([]Schedule{dailyWithRequires("gmail")}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	sch, _ := store.Get("s1")

	r := NewRunner(store, &fakeGateway{busy: true}, "device-1", nil)
	r.SetConnectorChecker(installed())

	rr, ok := r.RunNow(sch)
	if !ok || rr.Status != "skipped" {
		t.Fatalf("RunNow = (%+v, %v), want an immediate skipped report", rr, ok)
	}
}

// A one-shot ("once") task whose connector is missing is skipped exactly
// once and then spent: the skip consumes the only occurrence, NextRunAt goes
// to zero (never due), and no later tick fires or reports it again — the same
// end state a successful once reaches, rather than I5's retry loop.
func TestRunner_SkippedOnceScheduleIsReportedOnceAndNeverDueAgain(t *testing.T) {
	store := newTestStore(t)
	at := time.Date(2026, 8, 26, 8, 0, 0, 0, time.UTC)
	sch := Schedule{
		ID: "s1", Name: "Send the invoice", Instructions: "Email the invoice", Enabled: true,
		Cadence:  Spec{Repeat: RepeatOnce, At: &at},
		Requires: []string{"gmail"},
	}
	scheduledAt := seedJitteredSchedule(t, store, at.Add(-time.Hour), "device-1", sch)

	gw := &fakeGateway{}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.SetConnectorChecker(installed())

	for i := 0; i <= 45; i++ { // well past the 30-minute catch-up window
		r.tick(scheduledAt.Add(time.Duration(i) * time.Minute))
	}

	if len(gw.sent) != 0 {
		t.Fatalf("sent = %v, want the once task never handed to the agent", gw.sent)
	}
	if len(reports) != 1 || reports[0].Status != RunStatusSkipped {
		t.Fatalf("reports = %+v, want exactly one skipped ack", reports)
	}
	if !reports[0].NextRunAt.IsZero() {
		t.Errorf("report NextRunAt = %v, want zero (a spent once has no next occurrence)", reports[0].NextRunAt)
	}
	got, _ := store.Get("s1")
	if !got.NextRunAt.IsZero() {
		t.Errorf("NextRunAt = %v, want zero — a skipped once must never become due again", got.NextRunAt)
	}
	if got.LastRunStatus != RunStatusSkipped {
		t.Errorf("LastRunStatus = %q, want skipped", got.LastRunStatus)
	}
}

// The device's own Settings page renders WHY a run was skipped, from the
// persisted summary — so the ticker's skip must store it alongside the status.
func TestRunner_SkipPersistsItsSummary(t *testing.T) {
	store := newTestStore(t)
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", dailyWithRequires("gmail", "slack"))

	r := NewRunner(store, &fakeGateway{}, "device-1", nil)
	r.SetConnectorChecker(installed())
	r.tick(scheduledAt.Add(time.Minute))

	got, _ := store.Get("s1")
	if got.LastRunSummary != "missing connector: gmail, slack" {
		t.Fatalf("LastRunSummary = %q, want %q", got.LastRunSummary, "missing connector: gmail, slack")
	}
}

// Same for "Run now", which records through SetLastRun instead.
func TestRunNow_SkipPersistsItsSummary(t *testing.T) {
	store := newTestStore(t)
	if err := store.Replace([]Schedule{dailyWithRequires("gmail")}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	sch, _ := store.Get("s1")

	r := NewRunner(store, &fakeGateway{}, "device-1", nil)
	r.SetConnectorChecker(installed())
	if _, ok := r.RunNow(sch); !ok {
		t.Fatal("RunNow deferred a skip")
	}

	got, _ := store.Get("s1")
	if got.LastRunSummary != "missing connector: gmail" {
		t.Fatalf("LastRunSummary = %q, want %q", got.LastRunSummary, "missing connector: gmail")
	}
}

// Every last-run setter records the summary together with the status, in the
// same write, so the two can never disagree on disk.
func TestStore_LastRunSettersRecordSummary(t *testing.T) {
	store := newTestStore(t)
	if err := store.Replace([]Schedule{{ID: "a"}, {ID: "b"}, {ID: "c"}}); err != nil {
		t.Fatalf("replace: %v", err)
	}
	at := time.Date(2026, 8, 26, 8, 0, 0, 0, time.UTC)

	if err := store.SetLastRun("a", at, RunStatusSkipped, "missing connector: gmail"); err != nil {
		t.Fatalf("SetLastRun: %v", err)
	}
	if err := store.RecordRunResult("b", at, "success", "Daily briefing", at.Add(24*time.Hour)); err != nil {
		t.Fatalf("RecordRunResult: %v", err)
	}
	if err := store.SetLastFailedRun("c", at, "ws disconnected", at); err != nil {
		t.Fatalf("SetLastFailedRun: %v", err)
	}

	for id, want := range map[string]string{"a": "missing connector: gmail", "b": "Daily briefing", "c": "ws disconnected"} {
		got, _ := store.Get(id)
		if got.LastRunSummary != want {
			t.Errorf("%s: LastRunSummary = %q, want %q", id, got.LastRunSummary, want)
		}
	}
}

// LastRunSummary is device-local bookkeeping like LastRunStatus (the wire
// never carries it), so a schedule.sync must carry it forward — otherwise the
// Settings page would show "Skipped" with its reason wiped by the next sync.
func TestReplace_PreservesLastRunSummary(t *testing.T) {
	store := newTestStore(t)
	seeded := dailyWithRequires("gmail")
	seeded.LastRunStatus = RunStatusSkipped
	seeded.LastRunSummary = "missing connector: gmail"
	if err := store.Replace([]Schedule{seeded}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	if err := store.Replace([]Schedule{dailyWithRequires("gmail")}); err != nil {
		t.Fatalf("resync: %v", err)
	}
	got, _ := store.Get("s1")
	if got.LastRunSummary != "missing connector: gmail" {
		t.Fatalf("LastRunSummary = %q after a sync, want it carried forward", got.LastRunSummary)
	}
}
