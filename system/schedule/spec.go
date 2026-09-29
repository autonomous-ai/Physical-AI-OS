// Package schedule stores, computes and fires the device's scheduled tasks (MQTT schedule.sync),
// independent of the active agentic runtime.
package schedule

import (
	"hash/fnv"
	"log/slog"
	"strconv"
	"strings"
	"time"
)

// Repeat cadence values (wire values, do not rename).
const (
	RepeatDaily    = "daily"
	RepeatWeekly   = "weekly"
	RepeatMonthly  = "monthly"
	RepeatInterval = "interval"
	RepeatOnce     = "once"
	RepeatManual   = "manual"
)

// maxJitter bounds the per-device offset applied to daily/weekly/monthly occurrences.
const maxJitter = 5 * time.Minute

// Spec is the wire's nested "schedule" object, e.g. {"repeat":"weekly","days":[1,2,3,4,5],"time":"08:00"}.
// EndAt is copied in from the schedule's top-level end_at by Schedule.NextRun.
type Spec struct {
	Repeat string `json:"repeat"`

	// Days lists weekdays for "weekly": 0=Sunday..6=Saturday (7 also means Sunday).
	Days []int `json:"days,omitempty"`

	// DayOfMonth is 1-31 for "monthly"; short months clamp to their last day.
	DayOfMonth int `json:"day_of_month,omitempty"`

	// Time is the "HH:MM" (24h) fire time in tz; kept equal to Times[0] for older firmware.
	Time string `json:"time,omitempty"`

	// Times lists every daily fire time (cross product with Days); read via effectiveTimes.
	Times []string `json:"times,omitempty"`

	// EveryMs is the "interval" gap in milliseconds; not jittered, floored at minInterval.
	EveryMs uint64 `json:"every_ms,omitempty"`

	// At is the absolute fire time for "once"; never jittered.
	At *time.Time `json:"at,omitempty"`

	// EndAt is excluded from JSON; see the type doc.
	EndAt *time.Time `json:"-"`
}

// NextRun returns the earliest occurrence strictly after `after` in tz; false if it never fires again.
// Wall-clock math goes through time.Date so DST is resolved by the stdlib.
func (s Spec) NextRun(after time.Time, tz *time.Location) (time.Time, bool) {
	if s.EndAt != nil && !after.Before(*s.EndAt) {
		return time.Time{}, false
	}

	next, ok := s.nextOccurrence(after, tz)
	if !ok {
		return time.Time{}, false
	}
	if s.EndAt != nil && next.After(*s.EndAt) {
		return time.Time{}, false
	}
	return next, true
}

func (s Spec) nextOccurrence(after time.Time, tz *time.Location) (time.Time, bool) {
	switch s.Repeat {
	case RepeatDaily:
		return s.nextDaily(after, tz)
	case RepeatWeekly:
		return s.nextWeekly(after, tz)
	case RepeatMonthly:
		return s.nextMonthly(after, tz)
	case RepeatInterval:
		return s.nextInterval(after)
	case RepeatOnce:
		return s.nextOnce(after)
	case RepeatManual:
		return time.Time{}, false
	default:
		// Unknown repeat value: fail closed.
		return time.Time{}, false
	}
}

// parseTime parses "HH:MM"; ok=false on malformed input.
func parseTime(hhmm string) (hour, minute int, ok bool) {
	parts := strings.SplitN(hhmm, ":", 2)
	if len(parts) != 2 {
		return 0, 0, false
	}
	h, err1 := strconv.Atoi(parts[0])
	m, err2 := strconv.Atoi(parts[1])
	if err1 != nil || err2 != nil || h < 0 || h > 23 || m < 0 || m > 59 {
		return 0, 0, false
	}
	return h, m, true
}

// effectiveTimes returns Times, or the single Time for schedules stored before Times existed.
func (s Spec) effectiveTimes() []string {
	if len(s.Times) > 0 {
		return s.Times
	}
	if s.Time != "" {
		return []string{s.Time}
	}
	return nil
}

// earliestAcross returns the soonest result of next over all effective times, skipping unparseable ones.
func (s Spec) earliestAcross(next func(h, m int) (time.Time, bool)) (time.Time, bool) {
	var best time.Time
	found := false
	for _, raw := range s.effectiveTimes() {
		h, m, ok := parseTime(raw)
		if !ok {
			continue
		}
		candidate, ok := next(h, m)
		if !ok {
			continue
		}
		if !found || candidate.Before(best) {
			best, found = candidate, true
		}
	}
	return best, found
}

func (s Spec) nextDaily(after time.Time, tz *time.Location) (time.Time, bool) {
	return s.earliestAcross(func(h, m int) (time.Time, bool) {
		local := after.In(tz)
		for dayOffset := 0; dayOffset <= 1; dayOffset++ {
			candidate := time.Date(local.Year(), local.Month(), local.Day()+dayOffset, h, m, 0, 0, tz)
			if candidate.After(after) {
				return candidate, true
			}
		}
		return time.Time{}, false
	})
}

// normalizeWeekday maps a wire day (0=Sunday..6, 7=Sunday) to time.Weekday.
func normalizeWeekday(d int) time.Weekday {
	if d == 7 {
		return time.Sunday
	}
	return time.Weekday(d)
}

// nextWeekly scans up to 7 days ahead (via time.Date) for a listed weekday.
func (s Spec) nextWeekly(after time.Time, tz *time.Location) (time.Time, bool) {
	if len(s.Days) == 0 {
		return time.Time{}, false
	}
	wanted := make(map[time.Weekday]bool, len(s.Days))
	for _, d := range s.Days {
		wanted[normalizeWeekday(d)] = true
	}
	return s.earliestAcross(func(h, m int) (time.Time, bool) {
		local := after.In(tz)
		for dayOffset := 0; dayOffset <= 7; dayOffset++ {
			candidate := time.Date(local.Year(), local.Month(), local.Day()+dayOffset, h, m, 0, 0, tz)
			if !wanted[candidate.Weekday()] {
				continue
			}
			if candidate.After(after) {
				return candidate, true
			}
		}
		return time.Time{}, false
	})
}

// lastDayOfMonth returns the number of days in month (day 0 of the next month).
func lastDayOfMonth(year int, month time.Month) int {
	return time.Date(year, month+1, 0, 0, 0, 0, 0, time.UTC).Day()
}

// nextMonthly finds the next clamped DayOfMonth, searching up to 13 months ahead.
func (s Spec) nextMonthly(after time.Time, tz *time.Location) (time.Time, bool) {
	if s.DayOfMonth < 1 || s.DayOfMonth > 31 {
		return time.Time{}, false
	}
	return s.earliestAcross(func(h, m int) (time.Time, bool) {
		local := after.In(tz)
		year, month := local.Year(), local.Month()
		for i := 0; i < 13; i++ {
			total := int(month) - 1 + i
			y := year + total/12
			mo := time.Month(total%12 + 1)
			day := s.DayOfMonth
			if last := lastDayOfMonth(y, mo); day > last {
				day = last
			}
			candidate := time.Date(y, mo, day, h, m, 0, 0, tz)
			if candidate.After(after) {
				return candidate, true
			}
		}
		return time.Time{}, false
	})
}

// minInterval floors "interval" schedules: the send path has no rate limit, so a tiny
// every_ms would otherwise spam paid LLM turns every tick.
const minInterval = 5 * time.Minute

// nextInterval returns after + EveryMs (clamped to minInterval).
func (s Spec) nextInterval(after time.Time) (time.Time, bool) {
	if s.EveryMs == 0 {
		return time.Time{}, false
	}
	interval := time.Duration(s.EveryMs) * time.Millisecond
	if interval < minInterval {
		slog.Warn("schedule: interval below the floor, clamping",
			"component", "schedule", "requested", interval, "floor", minInterval)
		interval = minInterval
	}
	return after.Add(interval), true
}

// nextOnce fires exactly once, at At, and never again once At has passed.
func (s Spec) nextOnce(after time.Time) (time.Time, bool) {
	if s.At == nil || !s.At.After(after) {
		return time.Time{}, false
	}
	return *s.At, true
}

// JitterOffset deterministically maps (deviceID, scheduleID) to an offset in [-maxJitter, +maxJitter).
// Hashes to whole seconds: a uint32 modulo the nanosecond span would be a no-op.
// Not keyed per time, so DejitterAnchor stays reversible.
func JitterOffset(deviceID, scheduleID string) time.Duration {
	h := fnv.New32a()
	_, _ = h.Write([]byte(deviceID + "\x00" + scheduleID))
	sum := h.Sum32()
	spanSeconds := int64(2 * maxJitter / time.Second)
	secs := int64(sum) % spanSeconds
	return time.Duration(secs)*time.Second - maxJitter
}

// jitteredRepeat reports whether repeat is jittered; shared by NextRunForDevice and DejitterAnchor.
func jitteredRepeat(repeat string) bool {
	switch repeat {
	case RepeatDaily, RepeatWeekly, RepeatMonthly:
		return true
	default:
		return false
	}
}

// NextRunForDevice is Spec.NextRun plus jitter for daily/weekly/monthly (interval and once are exact).
func NextRunForDevice(spec Spec, after time.Time, tz *time.Location, deviceID, scheduleID string) (time.Time, bool) {
	next, ok := spec.NextRun(after, tz)
	if !ok {
		return time.Time{}, false
	}
	if jitteredRepeat(spec.Repeat) {
		next = next.Add(JitterOffset(deviceID, scheduleID))
	}
	return next, true
}

// DejitterAnchor removes the jitter from a stored occurrence. Feed its result, not the jittered
// value, back into NextRun: a negative jitter would otherwise return the same occurrence again.
func DejitterAnchor(repeat string, jitteredAt time.Time, deviceID, scheduleID string) time.Time {
	if !jitteredRepeat(repeat) {
		return jitteredAt
	}
	return jitteredAt.Add(-JitterOffset(deviceID, scheduleID))
}
