package schedule

import (
	"path/filepath"
	"testing"
	"time"
)

// A manual schedule syncs cleanly and is omitted from next_run_at.
func TestSyncSchedules_ManualScheduleAppliesCleanly(t *testing.T) {
	store := NewStore(filepath.Join(t.TempDir(), "schedules.json"))
	now := time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC)

	applied, nextRunAt, err := SyncSchedules(store, []Schedule{
		{ID: "s1", Enabled: true, Cadence: Spec{Repeat: RepeatManual}},
	}, "UTC", "device-1", now)
	if err != nil {
		t.Fatalf("SyncSchedules: %v", err)
	}
	if applied != 1 {
		t.Errorf("applied = %d, want 1", applied)
	}
	if _, ok := nextRunAt["s1"]; ok {
		t.Error("a manual schedule must not appear in next_run_at")
	}
}

// A spent once schedule syncs cleanly.
func TestSyncSchedules_SpentOnceAppliesCleanly(t *testing.T) {
	store := NewStore(filepath.Join(t.TempDir(), "schedules.json"))
	now := time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC)
	past := now.Add(-time.Hour)

	applied, nextRunAt, err := SyncSchedules(store, []Schedule{
		{ID: "s1", Enabled: true, Cadence: Spec{Repeat: RepeatOnce, At: &past}},
	}, "UTC", "device-1", now)
	if err != nil {
		t.Fatalf("SyncSchedules: %v", err)
	}
	if applied != 1 {
		t.Errorf("applied = %d, want 1", applied)
	}
	if _, ok := nextRunAt["s1"]; ok {
		t.Error("a spent once schedule must not appear in next_run_at")
	}
}

// TestExpectedNeverFires pins which never-firing schedules are expected.
func TestExpectedNeverFires(t *testing.T) {
	now := time.Date(2026, 8, 26, 7, 0, 0, 0, time.UTC)
	past := now.Add(-time.Hour)
	future := now.Add(time.Hour)

	cases := []struct {
		name string
		sch  Schedule
		want bool
	}{
		{"manual", Schedule{Cadence: Spec{Repeat: RepeatManual}}, true},
		{"spent once", Schedule{Cadence: Spec{Repeat: RepeatOnce, At: &past}}, true},
		{"once exactly at now is spent", Schedule{Cadence: Spec{Repeat: RepeatOnce, At: &now}}, true},
		{"future once is not spent", Schedule{Cadence: Spec{Repeat: RepeatOnce, At: &future}}, false},
		{"once missing At is malformed, must still warn", Schedule{Cadence: Spec{Repeat: RepeatOnce}}, false},
		{"malformed daily time must still warn", Schedule{Cadence: Spec{Repeat: RepeatDaily, Time: "garbage"}}, false},
		{"weekly with no days must still warn", Schedule{Cadence: Spec{Repeat: RepeatWeekly}}, false},
		{"monthly with an invalid day_of_month must still warn", Schedule{Cadence: Spec{Repeat: RepeatMonthly, DayOfMonth: 99}}, false},
		{"interval with every_ms=0 must still warn", Schedule{Cadence: Spec{Repeat: RepeatInterval}}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := expectedNeverFires(tc.sch, now); got != tc.want {
				t.Errorf("expectedNeverFires(%+v, %v) = %v, want %v", tc.sch, now, got, tc.want)
			}
		})
	}
}
