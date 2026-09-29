package schedule

import (
	"fmt"
	"testing"
	"time"
)

func mustLoadLocation(t *testing.T, name string) *time.Location {
	t.Helper()
	loc, err := time.LoadLocation(name)
	if err != nil {
		t.Fatalf("LoadLocation(%q): %v", name, err)
	}
	return loc
}

func TestNextRun_Daily(t *testing.T) {
	tz := mustLoadLocation(t, "Asia/Ho_Chi_Minh")
	spec := Spec{Repeat: RepeatDaily, Time: "08:00"}

	after := time.Date(2026, 8, 26, 7, 0, 0, 0, tz)
	got, ok := spec.NextRun(after, tz)
	if !ok {
		t.Fatal("expected ok=true")
	}
	want := time.Date(2026, 8, 26, 8, 0, 0, 0, tz)
	if !got.Equal(want) {
		t.Errorf("got %v, want %v", got, want)
	}

	after2 := time.Date(2026, 8, 26, 8, 0, 0, 0, tz)
	got2, ok2 := spec.NextRun(after2, tz)
	if !ok2 {
		t.Fatal("expected ok=true")
	}
	want2 := time.Date(2026, 8, 27, 8, 0, 0, 0, tz)
	if !got2.Equal(want2) {
		t.Errorf("got %v, want %v (must not re-fire the exact instant just passed)", got2, want2)
	}
}

// A 02:30 daily on the spring-forward day (LA, 2026-03-08) yields one occurrence per day.
func TestNextRun_DailyAcrossDSTSpringForward(t *testing.T) {
	tz := mustLoadLocation(t, "America/Los_Angeles")
	spec := Spec{Repeat: RepeatDaily, Time: "02:30"}

	after := time.Date(2026, 3, 7, 12, 0, 0, 0, tz)
	got, ok := spec.NextRun(after, tz)
	if !ok {
		t.Fatal("expected ok=true")
	}
	if got.Month() != time.March || got.Day() != 8 {
		t.Fatalf("expected the gap day itself (Mar 8), got %v", got)
	}

	got2, ok2 := spec.NextRun(got, tz)
	if !ok2 {
		t.Fatal("expected ok=true")
	}
	if got2.Day() != 9 {
		t.Fatalf("fired twice on the gap day: second occurrence = %v, want Mar 9", got2)
	}
}

// A 01:30 daily on the fall-back day (LA, 2026-11-01) fires once, not twice.
func TestNextRun_DailyAcrossDSTFallBack(t *testing.T) {
	tz := mustLoadLocation(t, "America/Los_Angeles")
	spec := Spec{Repeat: RepeatDaily, Time: "01:30"}

	after := time.Date(2026, 10, 31, 12, 0, 0, 0, tz)
	got, ok := spec.NextRun(after, tz)
	if !ok {
		t.Fatal("expected ok=true")
	}
	if got.Month() != time.November || got.Day() != 1 {
		t.Fatalf("expected the fall-back day itself (Nov 1), got %v", got)
	}

	got2, ok2 := spec.NextRun(got, tz)
	if !ok2 {
		t.Fatal("expected ok=true")
	}
	if got2.Day() != 2 {
		t.Fatalf("fired twice on the fall-back day: second occurrence = %v, want Nov 2", got2)
	}
}

func TestNextRun_WeeklyPicksNextListedDay(t *testing.T) {
	tz := mustLoadLocation(t, "Asia/Ho_Chi_Minh")
	spec := Spec{Repeat: RepeatWeekly, Days: []int{1, 2, 3, 4, 5}, Time: "08:00"}

	after := time.Date(2026, 8, 28, 9, 0, 0, 0, tz)
	if after.Weekday() != time.Friday {
		t.Fatalf("test fixture bug: %v is not a Friday", after)
	}
	got, ok := spec.NextRun(after, tz)
	if !ok {
		t.Fatal("expected ok=true")
	}
	want := time.Date(2026, 8, 31, 8, 0, 0, 0, tz)
	if !got.Equal(want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

// Days uses 0=Sunday..6=Saturday (7 also Sunday), not ISO-8601.
func TestNextRun_WeeklySundayConvention(t *testing.T) {
	tz := time.UTC
	// 2026-08-24 is a Monday.
	after := time.Date(2026, 8, 24, 9, 0, 0, 0, tz)
	if after.Weekday() != time.Monday {
		t.Fatalf("test fixture bug: %v is not a Monday", after)
	}
	want := time.Date(2026, 8, 30, 8, 0, 0, 0, tz)

	for _, sundayValue := range []int{0, 7} {
		spec := Spec{Repeat: RepeatWeekly, Days: []int{sundayValue}, Time: "08:00"}
		got, ok := spec.NextRun(after, tz)
		if !ok {
			t.Fatalf("days=[%d]: expected ok=true", sundayValue)
		}
		if !got.Equal(want) {
			t.Errorf("days=[%d]: got %v, want %v (canonical Sunday=0, with 7 accepted as an alias)", sundayValue, got, want)
		}
	}
}

// day_of_month=31 clamps to Feb 28 (29 in a leap year).
func TestNextRun_MonthlyClampsToLastDayOfShortMonth(t *testing.T) {
	tz := time.UTC
	spec := Spec{Repeat: RepeatMonthly, DayOfMonth: 31, Time: "09:00"}

	after := time.Date(2026, 1, 31, 10, 0, 0, 0, tz) // already past Jan 31 09:00
	got, ok := spec.NextRun(after, tz)
	if !ok {
		t.Fatal("expected ok=true")
	}
	want := time.Date(2026, 2, 28, 9, 0, 0, 0, tz) // 2026 is not a leap year
	if !got.Equal(want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestNextRun_MonthlyClampsToLastDayOfLeapFebruary(t *testing.T) {
	tz := time.UTC
	spec := Spec{Repeat: RepeatMonthly, DayOfMonth: 31, Time: "09:00"}

	after := time.Date(2028, 1, 31, 10, 0, 0, 0, tz) // 2028 IS a leap year
	got, ok := spec.NextRun(after, tz)
	if !ok {
		t.Fatal("expected ok=true")
	}
	want := time.Date(2028, 2, 29, 9, 0, 0, 0, tz)
	if !got.Equal(want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestNextRun_IntervalAnchorsOnLastRun(t *testing.T) {
	spec := Spec{Repeat: RepeatInterval, EveryMs: 1_800_000} // 30 minutes, in milliseconds per the wire contract
	lastRun := time.Date(2026, 8, 26, 10, 0, 0, 0, time.UTC)

	got, ok := spec.NextRun(lastRun, time.UTC)
	if !ok {
		t.Fatal("expected ok=true")
	}
	want := lastRun.Add(30 * time.Minute)
	if !got.Equal(want) {
		t.Errorf("got %v, want %v (anchored on the passed-in last run, not wall clock)", got, want)
	}
}

// every_ms below 5 minutes clamps to minInterval.
func TestNextRun_IntervalClampsToFiveMinuteFloor(t *testing.T) {
	spec := Spec{Repeat: RepeatInterval, EveryMs: 1000} // 1 second — far under the floor
	lastRun := time.Date(2026, 8, 26, 10, 0, 0, 0, time.UTC)

	got, ok := spec.NextRun(lastRun, time.UTC)
	if !ok {
		t.Fatal("expected ok=true")
	}
	want := lastRun.Add(5 * time.Minute)
	if !got.Equal(want) {
		t.Errorf("got %v, want %v (clamped to the 5-minute floor)", got, want)
	}
}

func TestNextRun_IntervalZeroNeverFires(t *testing.T) {
	spec := Spec{Repeat: RepeatInterval, EveryMs: 0}
	if _, ok := spec.NextRun(time.Now(), time.UTC); ok {
		t.Error("every_ms=0 must never fire")
	}
}

func TestNextRun_OnceReturnsFalseAfterItHasPassed(t *testing.T) {
	at := time.Date(2026, 8, 26, 8, 0, 0, 0, time.UTC)
	spec := Spec{Repeat: RepeatOnce, At: &at}

	before := at.Add(-time.Hour)
	got, ok := spec.NextRun(before, time.UTC)
	if !ok || !got.Equal(at) {
		t.Fatalf("NextRun(before) = %v, %v; want %v, true", got, ok, at)
	}

	if _, ok := spec.NextRun(at, time.UTC); ok {
		t.Error("a once schedule must not fire again once At has been reached")
	}
	after := at.Add(time.Hour)
	if _, ok := spec.NextRun(after, time.UTC); ok {
		t.Error("a once schedule must not fire again after At has passed")
	}
}

func TestNextRun_ManualNeverFires(t *testing.T) {
	now := time.Now()
	_, ok := Spec{Repeat: RepeatManual}.NextRun(now, time.UTC)
	if ok {
		t.Error("a manual schedule must never report a next run")
	}
}

func TestNextRun_PastEndAtReturnsFalse(t *testing.T) {
	tz := time.UTC
	endAt := time.Date(2026, 8, 1, 0, 0, 0, 0, tz)

	spec := Spec{Repeat: RepeatDaily, Time: "08:00", EndAt: &endAt}
	after := time.Date(2026, 8, 26, 7, 0, 0, 0, tz)
	if _, ok := spec.NextRun(after, tz); ok {
		t.Error("a schedule whose end_at has already passed must not fire")
	}

	endAtSoon := time.Date(2026, 8, 26, 7, 30, 0, 0, tz)
	spec2 := Spec{Repeat: RepeatDaily, Time: "08:00", EndAt: &endAtSoon}
	if _, ok := spec2.NextRun(time.Date(2026, 8, 26, 7, 0, 0, 0, tz), tz); ok {
		t.Error("a computed occurrence past end_at must not be returned")
	}
}

// Jitter is deterministic per (device, schedule) and differs across devices.
func TestNextRun_JitterIsDeterministicPerDevice(t *testing.T) {
	a1 := JitterOffset("device-A", "sched-1")
	a2 := JitterOffset("device-A", "sched-1")
	if a1 != a2 {
		t.Fatalf("same (device, schedule) produced different offsets: %v vs %v", a1, a2)
	}
	if a1 < -maxJitter || a1 > maxJitter {
		t.Fatalf("offset %v out of range [-%v, %v]", a1, maxJitter, maxJitter)
	}

	b1 := JitterOffset("device-B", "sched-1")
	if a1 == b1 {
		t.Errorf("different device ids produced the same offset (%v) for the same schedule — thundering-herd guard defeated", a1)
	}
}

// JitterOffset must spread across minutes, not collapse to a constant (uint32 vs nanoseconds bug).
func TestJitterOffset_SpansSeveralMinutesAcrossManyDevices(t *testing.T) {
	minOffset, maxOffset := maxJitter, -maxJitter
	for i := 0; i < 200; i++ {
		off := JitterOffset(fmt.Sprintf("device-%d", i), "sched-1")
		if off < -maxJitter || off >= maxJitter {
			t.Fatalf("device-%d: offset %v out of range [-%v, %v)", i, off, maxJitter, maxJitter)
		}
		if off < minOffset {
			minOffset = off
		}
		if off > maxOffset {
			maxOffset = off
		}
	}
	if spread := maxOffset - minOffset; spread < 2*time.Minute {
		t.Fatalf("jitter spread across 200 device ids = %v (min=%v max=%v), want at least 2 minutes", spread, minOffset, maxOffset)
	}
}

func TestNextRunForDevice_AppliesJitterOnlyToWallClockCadences(t *testing.T) {
	tz := time.UTC
	after := time.Date(2026, 8, 26, 7, 0, 0, 0, tz)

	daily := Spec{Repeat: RepeatDaily, Time: "08:00"}
	base, _ := daily.NextRun(after, tz)
	jittered, ok := NextRunForDevice(daily, after, tz, "device-1", "sched-1")
	if !ok {
		t.Fatal("expected ok=true")
	}
	wantOffset := JitterOffset("device-1", "sched-1")
	if !jittered.Equal(base.Add(wantOffset)) {
		t.Errorf("jittered = %v, want base %v + offset %v", jittered, base, wantOffset)
	}

	lastRun := time.Date(2026, 8, 26, 10, 0, 0, 0, tz)
	interval := Spec{Repeat: RepeatInterval, EveryMs: 600_000} // 10 minutes — above the 5-minute floor
	gotInterval, ok := NextRunForDevice(interval, lastRun, tz, "device-1", "sched-2")
	if !ok {
		t.Fatal("expected ok=true")
	}
	if !gotInterval.Equal(lastRun.Add(10 * time.Minute)) {
		t.Errorf("interval schedule was jittered: got %v", gotInterval)
	}

	at := time.Date(2026, 8, 27, 0, 0, 0, 0, tz)
	once := Spec{Repeat: RepeatOnce, At: &at}
	gotOnce, ok := NextRunForDevice(once, after, tz, "device-1", "sched-3")
	if !ok {
		t.Fatal("expected ok=true")
	}
	if !gotOnce.Equal(at) {
		t.Errorf("once schedule was jittered: got %v, want exactly %v", gotOnce, at)
	}
}

// DejitterAnchor exactly reverses NextRunForDevice's jitter.
func TestDejitterAnchor_ReversesNextRunForDeviceJitter(t *testing.T) {
	tz := time.UTC
	after := time.Date(2026, 8, 26, 7, 0, 0, 0, tz)

	for _, repeat := range []string{RepeatDaily, RepeatWeekly, RepeatMonthly} {
		spec := Spec{Repeat: repeat, Time: "08:00", Days: []int{3}, DayOfMonth: 15}
		base, ok := spec.NextRun(after, tz)
		if !ok {
			t.Fatalf("%s: expected ok=true", repeat)
		}
		jittered, ok := NextRunForDevice(spec, after, tz, "device-1", "sched-1")
		if !ok {
			t.Fatalf("%s: expected ok=true", repeat)
		}
		dejittered := DejitterAnchor(repeat, jittered, "device-1", "sched-1")
		if !dejittered.Equal(base) {
			t.Errorf("%s: DejitterAnchor(%v) = %v, want the unjittered base %v", repeat, jittered, dejittered, base)
		}
	}

	for _, repeat := range []string{RepeatInterval, RepeatOnce, RepeatManual} {
		stamp := time.Date(2026, 8, 26, 12, 0, 0, 0, tz)
		if got := DejitterAnchor(repeat, stamp, "device-1", "sched-1"); !got.Equal(stamp) {
			t.Errorf("%s: DejitterAnchor must be a no-op, got %v, want %v", repeat, got, stamp)
		}
	}
}
