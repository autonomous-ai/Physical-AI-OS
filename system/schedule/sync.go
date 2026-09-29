package schedule

import (
	"log/slog"
	"strings"
	"time"
)

// ResolveTimezone resolves an IANA zone name; blank or invalid falls back to UTC (with the error).
func ResolveTimezone(name string) (*time.Location, error) {
	name = strings.TrimSpace(name)
	if name == "" {
		return time.UTC, nil
	}
	loc, err := time.LoadLocation(name)
	if err != nil {
		return time.UTC, err
	}
	return loc, nil
}

// SyncSchedules applies a full schedule.sync (schedules + timezone in one write), then persists
// NextRunAt for each enabled schedule and returns the computed next runs keyed by id.
func SyncSchedules(store *Store, schedules []Schedule, timezone, deviceID string, now time.Time) (applied int, nextRunAt map[string]time.Time, err error) {
	tz, tzErr := ResolveTimezone(timezone)
	if tzErr != nil {
		// Bad tz: keep the schedules with UTC math rather than dropping them.
		slog.Warn("schedule.sync: invalid timezone, falling back to UTC",
			"component", "schedule", "timezone", timezone, "error", tzErr)
	}

	if err := store.ReplaceWithTimezone(schedules, timezone); err != nil {
		return 0, nil, err
	}

	applied = len(schedules)
	nextRunAt = make(map[string]time.Time, applied)
	for _, sch := range schedules {
		if !sch.Enabled {
			continue
		}
		next, ok := sch.NextRun(now, tz, deviceID)
		if !ok {
			// Manual and spent once schedules never fire by design; warn only on the unexpected cases.
			if !expectedNeverFires(sch, now) {
				slog.Warn("schedule.sync: schedule cannot be computed, skipping next_run_at",
					"component", "schedule", "id", sch.ID, "repeat", sch.Cadence.Repeat)
			}
			continue
		}
		nextRunAt[sch.ID] = next
		// Persist now so the next tick agrees with the acked (jittered) value.
		if err := store.SetNextRun(sch.ID, next); err != nil {
			slog.Error("schedule.sync: persist next_run_at failed",
				"component", "schedule", "id", sch.ID, "error", err)
		}
	}
	return applied, nextRunAt, nil
}

// expectedNeverFires reports whether never firing is by design (manual, or a spent once).
func expectedNeverFires(sch Schedule, now time.Time) bool {
	switch sch.Cadence.Repeat {
	case RepeatManual:
		return true
	case RepeatOnce:
		return sch.Cadence.At != nil && !sch.Cadence.At.After(now)
	default:
		return false
	}
}
