package schedule

import (
	"errors"
	"path/filepath"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// fakeGateway implements only the AgentGateway methods the Runner calls; others panic.
type fakeGateway struct {
	domain.AgentGateway
	busy          bool
	setBusyOnSend bool // simulate the WS lifecycle marking the agent busy the instant a turn starts
	sendErr       error
	sent          []string

	// spoken records Speak() calls, separate from sent to verify the transport.
	spoken   []string
	speakErr error
}

func (f *fakeGateway) IsBusy() bool { return f.busy }

func (f *fakeGateway) Speak(text string) error {
	f.spoken = append(f.spoken, text)
	if f.setBusyOnSend {
		f.busy = true
	}
	return f.speakErr
}

func (f *fakeGateway) SendSystemChatMessage(msg string) (string, error) {
	f.sent = append(f.sent, msg)
	if f.setBusyOnSend {
		f.busy = true
	}
	if f.sendErr != nil {
		return "", f.sendErr
	}
	return "ok", nil
}

func newTestStore(t *testing.T) *Store {
	t.Helper()
	return NewStore(filepath.Join(t.TempDir(), "schedules.json"))
}

// seedJitteredSchedule stores schedules via SyncSchedules so NextRunAt carries real jitter.
// Returns the NextRunAt of the last schedule.
func seedJitteredSchedule(t *testing.T, store *Store, computedFrom time.Time, deviceID string, schedules ...Schedule) time.Time {
	t.Helper()
	_, nextRunAt, err := SyncSchedules(store, schedules, "UTC", deviceID, computedFrom)
	if err != nil {
		t.Fatalf("SyncSchedules: %v", err)
	}
	last := schedules[len(schedules)-1]
	next, ok := nextRunAt[last.ID]
	if !ok {
		t.Fatalf("SyncSchedules did not compute next_run_at for %s", last.ID)
	}
	return next
}

func TestRunner_FiresDueScheduleViaSendSystemChatMessage(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Name: "Daily briefing", Instructions: "Say the daily briefing", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)
	now := scheduledAt.Add(time.Minute) // a normal, barely-overdue tick

	gw := &fakeGateway{}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.tick(now)

	if want := sch.Instructions + "\n[via:schedule]"; len(gw.sent) != 1 || gw.sent[0] != want {
		t.Fatalf("sent = %v, want exactly [%q]", gw.sent, want)
	}
	if len(reports) != 1 || reports[0].Status != "success" || reports[0].ScheduleID != "s1" {
		t.Fatalf("reports = %+v", reports)
	}

	got, _ := store.Get("s1")
	if !got.NextRunAt.After(now) {
		t.Errorf("NextRunAt not advanced past the fire: %v", got.NextRunAt)
	}
	if got.LastRunStatus != "success" {
		t.Errorf("LastRunStatus = %q, want success", got.LastRunStatus)
	}
}

// RunReport.NextRunAt must equal the next occurrence fire() persisted.
func TestRunner_ReportsFreshlyComputedNextRunAt(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Name: "Daily briefing", Instructions: "Say the daily briefing", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)
	now := scheduledAt.Add(time.Minute)

	gw := &fakeGateway{}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.tick(now)

	if len(reports) != 1 {
		t.Fatalf("reports = %+v, want exactly 1", reports)
	}
	if reports[0].NextRunAt.IsZero() {
		t.Fatal("RunReport.NextRunAt is zero — the freshly computed occurrence was never forwarded to the ack")
	}

	got, _ := store.Get("s1")
	if !reports[0].NextRunAt.Equal(got.NextRunAt) {
		t.Errorf("RunReport.NextRunAt = %v, want it to match the persisted NextRunAt %v", reports[0].NextRunAt, got.NextRunAt)
	}
}

// RunID is the gateway's returned id and Summary is the schedule name on success.
func TestRunner_ReportsGatewayRunIDNotLocalID(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Name: "Daily briefing", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)
	now := scheduledAt.Add(time.Minute)

	gw := &fakeGateway{} // SendSystemChatMessage returns "ok" as its run id
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.tick(now)

	if len(reports) != 1 {
		t.Fatalf("reports = %+v", reports)
	}
	got := reports[0]
	if got.RunID != "ok" {
		t.Errorf("RunID = %q, want the gateway's own returned run id (\"ok\"), not a locally fabricated one", got.RunID)
	}
	if got.Summary != "Daily briefing" {
		t.Errorf("Summary on success = %q, want the schedule name", got.Summary)
	}
}

// On send failure RunID is empty and Summary carries the error.
func TestRunner_ReportsSendErrorAsSummaryWithEmptyRunID(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Name: "Daily briefing", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)
	now := scheduledAt.Add(time.Minute)

	gw := &fakeGateway{sendErr: errors.New("ws disconnected")}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.tick(now)

	if len(reports) != 1 {
		t.Fatalf("reports = %+v", reports)
	}
	got := reports[0]
	if got.Status != "failure" || got.Summary != "ws disconnected" {
		t.Errorf("got %+v", got)
	}
	if got.RunID != "" {
		t.Errorf("RunID on a failed send = %q, want empty (no run actually started)", got.RunID)
	}
}

// A send failure must not advance NextRunAt (I5).
func TestRunner_SendFailureDoesNotAdvanceNextRunAt(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)
	now := scheduledAt.Add(time.Minute)

	gw := &fakeGateway{sendErr: errors.New("ws disconnected")}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	r.tick(now)

	got, _ := store.Get("s1")
	if !got.NextRunAt.Equal(scheduledAt) {
		t.Errorf("NextRunAt changed after a failed send: got %v, want untouched %v", got.NextRunAt, scheduledAt)
	}
	if got.LastRunStatus != "failure" {
		t.Errorf("LastRunStatus = %q, want failure recorded even though NextRunAt was left alone", got.LastRunStatus)
	}
	if !got.LastFailedOccurrence.Equal(scheduledAt) {
		t.Errorf("LastFailedOccurrence = %v, want it pinned to the occurrence %v", got.LastFailedOccurrence, scheduledAt)
	}
	if len(reports) != 1 {
		t.Fatalf("the first failure of an occurrence must ack: reports = %+v", reports)
	}

	r.tick(now.Add(time.Minute))
	if len(gw.sent) != 2 {
		t.Fatalf("sent = %v, want a retry attempt on the next tick", gw.sent)
	}
	if len(reports) != 1 {
		t.Fatalf("a retry of the SAME occurrence must not ack again: reports = %+v", reports)
	}
}

// Retries continue every tick, but only one failure ack is emitted per occurrence.
func TestRunner_SuppressesRepeatedFailureAcksWithinSameOccurrence(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)

	gw := &fakeGateway{sendErr: errors.New("ws disconnected")}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })

	// Minutes 0-30 attempt a send; minute 31 is past the window and re-anchors.
	for i := 0; i <= 31; i++ {
		r.tick(scheduledAt.Add(time.Duration(i) * time.Minute))
	}

	if len(gw.sent) != 31 {
		t.Fatalf("sent = %d attempts, want exactly 31 (one per tick within the 30-minute catch-up window) — the retry behaviour must NOT change", len(gw.sent))
	}
	if len(reports) != 1 {
		t.Fatalf("reports = %+v (%d acks), want exactly 1 for this one occurrence", reports, len(reports))
	}
	if reports[0].Status != "failure" {
		t.Errorf("reports[0].Status = %q, want failure", reports[0].Status)
	}
}

// A success acks even after suppressed failures in the same occurrence.
func TestRunner_SuccessAfterSuppressedFailuresStillAcks(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Name: "Daily briefing", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)

	gw := &fakeGateway{sendErr: errors.New("ws disconnected")}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })

	r.tick(scheduledAt)
	r.tick(scheduledAt.Add(time.Minute))
	if len(reports) != 1 || reports[0].Status != "failure" {
		t.Fatalf("after 2 failures, reports = %+v, want exactly 1 failure ack", reports)
	}

	gw.sendErr = nil
	r.tick(scheduledAt.Add(2 * time.Minute))
	if len(reports) != 2 || reports[1].Status != "success" {
		t.Fatalf("after recovery, reports = %+v, want a second (success) ack", reports)
	}
}

// After re-anchoring, the next occurrence's first failure acks again.
func TestRunner_NextOccurrenceAcksItsFirstFailureAgain(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)

	gw := &fakeGateway{sendErr: errors.New("ws disconnected")}
	var reports []RunReport
	r := NewRunner(store, gw, "device-1", func(rr RunReport) { reports = append(reports, rr) })

	for i := 0; i <= 31; i++ {
		r.tick(scheduledAt.Add(time.Duration(i) * time.Minute))
	}
	if len(reports) != 1 {
		t.Fatalf("first occurrence: reports = %+v, want exactly 1", reports)
	}

	got, _ := store.Get("s1")
	if got.NextRunAt.IsZero() {
		t.Fatal("expected a re-anchored NextRunAt for the next occurrence")
	}
	r.tick(got.NextRunAt)

	if len(reports) != 2 {
		t.Fatalf("second occurrence's first failure was wrongly suppressed: reports = %+v, want 2 total", reports)
	}
	if reports[1].Status != "failure" {
		t.Errorf("reports[1].Status = %q, want failure", reports[1].Status)
	}
}

// A fired occurrence must not re-fire; device-3/s1 has negative jitter, the case that broke.
func TestRunner_DoesNotReFireWithinTheSameOccurrence(t *testing.T) {
	const deviceID = "device-3"
	if off := JitterOffset(deviceID, "s1"); off >= 0 {
		t.Fatalf("test fixture bug: JitterOffset(%q, \"s1\") = %v, want negative (that's the case this test must exercise)", deviceID, off)
	}

	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 30, 0, 0, time.UTC), deviceID, sch)

	gw := &fakeGateway{}
	r := NewRunner(store, gw, deviceID, nil)

	for i := 0; i < 40; i++ {
		r.tick(scheduledAt.Add(time.Duration(i) * time.Minute))
	}

	if len(gw.sent) != 1 {
		t.Fatalf("sent = %v (%d messages) across 40 simulated minutes, want exactly 1", gw.sent, len(gw.sent))
	}
}

func TestRunner_DefersWhenGatewayBusy(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)
	now := scheduledAt.Add(time.Minute)

	gw := &fakeGateway{busy: true}
	r := NewRunner(store, gw, "device-1", nil)
	r.tick(now)

	if len(gw.sent) != 0 {
		t.Fatalf("must not send while the gateway is busy: sent = %v", gw.sent)
	}
	got, _ := store.Get("s1")
	if !got.NextRunAt.Equal(scheduledAt) {
		t.Errorf("NextRunAt changed while deferred: got %v, want untouched %v", got.NextRunAt, scheduledAt)
	}
}

// 29 minutes overdue fires exactly once.
func TestRunner_BootCatchUpFiresRecentlyOverdueOnce(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)
	now := scheduledAt.Add(29 * time.Minute)

	gw := &fakeGateway{}
	r := NewRunner(store, gw, "device-1", nil)
	r.tick(now)

	if len(gw.sent) != 1 {
		t.Fatalf("sent = %v, want exactly 1", gw.sent)
	}

	r.tick(now.Add(time.Minute))
	if len(gw.sent) != 1 {
		t.Fatalf("fired again on a later tick: sent = %v", gw.sent)
	}
}

// 31 minutes overdue is re-anchored forward, not fired.
func TestRunner_BootCatchUpSkipsStaleOverdue(t *testing.T) {
	store := newTestStore(t)
	sch := Schedule{
		ID: "s1", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
	}
	scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)
	now := scheduledAt.Add(31 * time.Minute)

	gw := &fakeGateway{}
	r := NewRunner(store, gw, "device-1", nil)
	r.tick(now)

	if len(gw.sent) != 0 {
		t.Fatalf("must not fire a stale overdue run: sent = %v", gw.sent)
	}
	got, _ := store.Get("s1")
	if !got.NextRunAt.After(now) {
		t.Errorf("NextRunAt not re-anchored forward: got %v, want after %v", got.NextRunAt, now)
	}
	if got.LastRunStatus != "" {
		t.Errorf("a re-anchor must not record a run: LastRunStatus = %q", got.LastRunStatus)
	}
}

// Two schedules due in one tick fire sequentially, never concurrently.
func TestRunner_SingleFlight(t *testing.T) {
	store := newTestStore(t)
	computedFrom := time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC)
	s1 := Schedule{ID: "s1", Instructions: "one", Enabled: true, Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"}}
	s2 := Schedule{ID: "s2", Instructions: "two", Enabled: true, Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"}}
	_, nextRunAt, err := SyncSchedules(store, []Schedule{s1, s2}, "UTC", "device-1", computedFrom)
	if err != nil {
		t.Fatalf("SyncSchedules: %v", err)
	}
	scheduledAt1, scheduledAt2 := nextRunAt["s1"], nextRunAt["s2"]
	now := scheduledAt1
	if scheduledAt2.After(now) {
		now = scheduledAt2
	}
	now = now.Add(time.Minute)

	gw := &fakeGateway{setBusyOnSend: true} // mimics the real WS lifecycle: busy flips true the moment a turn starts
	r := NewRunner(store, gw, "device-1", nil)
	r.tick(now)

	if len(gw.sent) != 1 || gw.sent[0] != "one\n[via:schedule]" {
		t.Fatalf("sent = %v, want exactly [\"one\"] in this tick — the second schedule must wait, not run concurrently", gw.sent)
	}
	got2, _ := store.Get("s2")
	if !got2.NextRunAt.Equal(scheduledAt2) {
		t.Errorf("deferred schedule's NextRunAt must be untouched: got %v, want %v", got2.NextRunAt, scheduledAt2)
	}

	gw.busy = false
	r.tick(now.Add(time.Minute))
	if len(gw.sent) != 2 || gw.sent[1] != "two\n[via:schedule]" {
		t.Fatalf("sent = %v, want the deferred schedule to run once the agent is free", gw.sent)
	}
}

func TestRunner_DisabledScheduleNeverFires(t *testing.T) {
	store := newTestStore(t)
	// SyncSchedules gives disabled rows no NextRunAt; set one to prove it still never fires.
	scheduledAt := time.Date(2026, 8, 26, 8, 0, 0, 0, time.UTC)
	now := scheduledAt.Add(time.Minute)

	if err := store.Replace([]Schedule{{
		ID: "s1", Instructions: "hi", Enabled: false,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"}, NextRunAt: scheduledAt,
	}}); err != nil {
		t.Fatalf("seed: %v", err)
	}

	gw := &fakeGateway{}
	r := NewRunner(store, gw, "device-1", nil)
	r.tick(now)

	if len(gw.sent) != 0 {
		t.Fatalf("a disabled schedule fired: sent = %v", gw.sent)
	}
}

func TestRunner_RunNowDefersWhenBusyAndDoesNotTouchNextRunAt(t *testing.T) {
	store := newTestStore(t)
	scheduledAt := time.Date(2026, 8, 27, 8, 0, 0, 0, time.UTC)
	if err := store.Replace([]Schedule{{
		ID: "s1", Name: "Daily briefing", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"}, NextRunAt: scheduledAt,
	}}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	sch, _ := store.Get("s1")

	busyGW := &fakeGateway{busy: true}
	r := NewRunner(store, busyGW, "device-1", nil)
	if _, ok := r.RunNow(sch); ok {
		t.Fatal("RunNow must defer (ok=false) while the gateway is busy")
	}
	if len(busyGW.sent) != 0 {
		t.Fatalf("must not send while busy: sent = %v", busyGW.sent)
	}

	freeGW := &fakeGateway{}
	var reports []RunReport
	r2 := NewRunner(store, freeGW, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	report, ok := r2.RunNow(sch)
	if !ok {
		t.Fatal("RunNow should have run against a free gateway")
	}
	if len(freeGW.sent) != 1 || freeGW.sent[0] != "hi\n[via:schedule]" {
		t.Fatalf("sent = %v", freeGW.sent)
	}
	if len(reports) != 1 {
		t.Fatalf("reports = %+v", reports)
	}
	if report.RunID != "ok" {
		t.Errorf("RunID = %q, want the gateway's own returned run id", report.RunID)
	}
	if report.Summary != "Daily briefing" {
		t.Errorf("Summary = %q, want the schedule name", report.Summary)
	}

	got, _ := store.Get("s1")
	if !got.NextRunAt.Equal(scheduledAt) {
		t.Errorf("RunNow must not change NextRunAt: got %v, want untouched %v", got.NextRunAt, scheduledAt)
	}
	if got.LastRunStatus != "success" {
		t.Errorf("LastRunStatus = %q, want success", got.LastRunStatus)
	}
}
