package schedule

import (
	"errors"
	"testing"
	"time"
)

// The report callback is the ONE seam everything downstream of a run hangs
// off (the schedule.run ack, and the ops alert). An alert has to name the task
// in every outcome — but Summary is the name only on success (it is the error
// on failure, the missing connectors on a skip) — so the report carries the
// name itself. And it has to say whether the run was a manual "Run now",
// which the callback cannot otherwise tell from a ticker fire.

func TestRunNow_ReportIsManualAndNamed(t *testing.T) {
	store := newTestStore(t)
	if err := store.Replace([]Schedule{{ID: "s1", Name: "Daily briefing", Instructions: "hi", Enabled: true,
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"}}}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	sch, _ := store.Get("s1")

	var reports []RunReport
	r := NewRunner(store, &fakeGateway{}, "device-1", func(rr RunReport) { reports = append(reports, rr) })
	rr, ok := r.RunNow(sch)
	if !ok {
		t.Fatal("RunNow deferred against a free gateway")
	}
	if !rr.Manual || rr.Name != "Daily briefing" {
		t.Fatalf("report = %+v, want Manual=true and Name=Daily briefing", rr)
	}
	if len(reports) != 1 || !reports[0].Manual {
		t.Fatalf("callback reports = %+v, want the Manual flag on the callback's copy too", reports)
	}
}

func TestRunNow_SkipReportIsManualAndNamed(t *testing.T) {
	store := newTestStore(t)
	if err := store.Replace([]Schedule{dailyWithRequires("gmail")}); err != nil {
		t.Fatalf("seed: %v", err)
	}
	sch, _ := store.Get("s1")

	r := NewRunner(store, &fakeGateway{}, "device-1", nil)
	r.SetConnectorChecker(installed())
	rr, _ := r.RunNow(sch)
	if rr.Status != RunStatusSkipped || !rr.Manual || rr.Name != "Inbox digest" {
		t.Fatalf("report = %+v, want a manual skipped report named Inbox digest", rr)
	}
}

func TestTicker_ReportsAreNotManualAndCarryTheName(t *testing.T) {
	cases := []struct {
		name     string
		requires []string
		sendErr  error
		status   string
	}{
		{"success", nil, nil, "success"},
		{"failure", nil, errors.New("ws disconnected"), "failure"},
		{"skipped", []string{"gmail"}, nil, RunStatusSkipped},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			store := newTestStore(t)
			sch := dailyWithRequires(tc.requires...)
			scheduledAt := seedJitteredSchedule(t, store, time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC), "device-1", sch)

			var reports []RunReport
			r := NewRunner(store, &fakeGateway{sendErr: tc.sendErr}, "device-1", func(rr RunReport) { reports = append(reports, rr) })
			r.SetConnectorChecker(installed())
			r.tick(scheduledAt.Add(time.Minute))

			if len(reports) != 1 {
				t.Fatalf("reports = %+v, want 1", reports)
			}
			got := reports[0]
			if got.Status != tc.status || got.Manual || got.Name != "Inbox digest" {
				t.Fatalf("report = %+v, want status %s, Manual=false, Name=Inbox digest", got, tc.status)
			}
		})
	}
}
