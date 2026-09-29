package schedule

import (
	"encoding/json"
	"errors"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// fakeConnectors is a ConnectorChecker double that records every code asked about.
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

// dailyWithRequires returns a due-able daily schedule with the given requirements.
func dailyWithRequires(requires ...string) Schedule {
	return Schedule{
		ID: "s1", Name: "Inbox digest", Instructions: "Summarize my unread email", Enabled: true,
		Cadence:  Spec{Repeat: RepeatDaily, Time: "08:00"},
		Requires: requires,
	}
}

// TestScheduleRequiresParsesFromSyncWire checks "requires" parses in wire order.
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

// TestScheduleWithoutRequiresOnWireHasNone checks rows without the key carry no requirement.
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

// TestScheduleRequiresSurvivesStoreRoundTripAndIsOmittedWhenEmpty checks persistence and omitempty.
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

// TestReplace_RequiresFollowsTheWireNotThePriorRow checks requires is not carried forward on sync.
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

// A missing required connector skips the run, reports "skipped" and advances NextRunAt.
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

// Tasks without requirements run normally and never consult the checker.
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

// A nil checker disables the guard.
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

// The skip summary lists only missing codes in requires order, ignoring blanks and repeats.
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

// Failure-ack suppression must not swallow a later skip of the same occurrence.
func TestRunner_SkipIsReportedEvenAfterAFailureAckInTheSameOccurrence(t *testing.T) {
	store := newTestStore(t)
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", dailyWithRequires("gmail"))

	gw := &fakeGateway{sendErr: errors.New("ws disconnected")}
	checker := installed("gmail")
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.SetConnectorChecker(checker)

	r.tick(scheduledAt)
	if len(reports) != 1 || reports[0].Status != "failure" {
		t.Fatalf("reports = %+v, want one failure ack", reports)
	}

	delete(checker.installed, "gmail")
	r.tick(scheduledAt.Add(time.Minute))
	if len(reports) != 2 || reports[1].Status != "skipped" {
		t.Fatalf("reports = %+v, want the skip reported after the suppressed-failure state", reports)
	}
	got, _ := store.Get("s1")
	if !got.NextRunAt.After(scheduledAt) {
		t.Errorf("NextRunAt = %v, want advanced past the skipped occurrence", got.NextRunAt)
	}
}

// Each skipped occurrence reports once.
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

// A skip is recorded at its due time even while the agent is busy.
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

// RunNow skips on a missing connector without touching NextRunAt.
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

// A manual skip answers immediately even while the agent is busy.
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

// A skipped once schedule reports once and is never due again.
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

// The ticker's skip persists its summary.
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

// RunNow's skip persists its summary.
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

// Every last-run setter records summary together with status.
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

// LastRunSummary is carried forward across a schedule.sync.
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
