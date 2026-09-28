package schedule

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// A write that fails mid-way must leave the previous file intact — Replace
// writes to a temp file in the same directory, then renames, so a failure
// before the rename can never touch the real file.
func TestStore_ReplaceIsAtomic(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "schedules.json")
	store := NewStore(path)

	original := []Schedule{{ID: "a", Name: "Original"}}
	if err := store.Replace(original); err != nil {
		t.Fatalf("seed replace: %v", err)
	}

	// Make the directory read-only so CreateTemp for the next Replace fails
	// before it ever gets to rename.
	if err := os.Chmod(dir, 0555); err != nil {
		t.Fatalf("chmod: %v", err)
	}
	t.Cleanup(func() { os.Chmod(dir, 0755) })

	err := store.Replace([]Schedule{{ID: "b", Name: "Should not land"}})
	if err == nil {
		t.Fatal("expected the write to fail against a read-only directory")
	}

	if err := os.Chmod(dir, 0755); err != nil { // restore so Load can read the dir back
		t.Fatalf("chmod restore: %v", err)
	}
	got, loadErr := store.Load()
	if loadErr != nil {
		t.Fatalf("load: %v", loadErr)
	}
	if len(got) != 1 || got[0].ID != "a" {
		t.Fatalf("the failed write corrupted or replaced the original file: %+v", got)
	}
}

// First boot has no schedules.json yet. That is not an error.
func TestStore_LoadMissingFileReturnsEmptyNotError(t *testing.T) {
	dir := t.TempDir()
	store := NewStore(filepath.Join(dir, "does-not-exist", "schedules.json"))

	got, err := store.Load()
	if err != nil {
		t.Fatalf("Load() err = %v, want nil", err)
	}
	if len(got) != 0 {
		t.Fatalf("Load() = %+v, want empty", got)
	}
}

// A truncated file after power loss must degrade to "no schedules", because
// the next schedule.sync repairs it — never panic, never a fatal boot error.
func TestStore_LoadCorruptFileReturnsEmptyAndDoesNotPanic(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "schedules.json")
	if err := os.WriteFile(path, []byte(`{"schedules":[{"id":"a", TRUNCATED`), 0644); err != nil {
		t.Fatalf("seed corrupt file: %v", err)
	}
	store := NewStore(path)

	got, err := store.Load()
	if err != nil {
		t.Fatalf("Load() err = %v, want nil (degrade, don't error)", err)
	}
	if len(got) != 0 {
		t.Fatalf("Load() = %+v, want empty for a corrupt file", got)
	}
}

func TestStore_GetFindsAndMisses(t *testing.T) {
	dir := t.TempDir()
	store := NewStore(filepath.Join(dir, "schedules.json"))
	if err := store.Replace([]Schedule{{ID: "a", Name: "A"}, {ID: "b", Name: "B"}}); err != nil {
		t.Fatalf("replace: %v", err)
	}

	got, ok := store.Get("b")
	if !ok || got.Name != "B" {
		t.Fatalf("Get(b) = %+v, %v", got, ok)
	}
	if _, ok := store.Get("nope"); ok {
		t.Error("Get(nope) = true, want false")
	}
}

func TestStore_SetLastRunLeavesNextRunAtUntouched(t *testing.T) {
	dir := t.TempDir()
	store := NewStore(filepath.Join(dir, "schedules.json"))
	next := time.Date(2026, 8, 27, 8, 0, 0, 0, time.UTC)
	if err := store.Replace([]Schedule{{ID: "a", NextRunAt: next}}); err != nil {
		t.Fatalf("replace: %v", err)
	}

	ranAt := time.Date(2026, 8, 26, 8, 0, 0, 0, time.UTC)
	if err := store.SetLastRun("a", ranAt, "success", "Daily briefing"); err != nil {
		t.Fatalf("SetLastRun: %v", err)
	}

	got, _ := store.Get("a")
	if !got.LastRunAt.Equal(ranAt) || got.LastRunStatus != "success" {
		t.Errorf("last run not recorded: %+v", got)
	}
	if !got.NextRunAt.Equal(next) {
		t.Errorf("NextRunAt = %v, want untouched %v", got.NextRunAt, next)
	}
}

func TestStore_RecordRunResultUpdatesBothFieldsTogether(t *testing.T) {
	dir := t.TempDir()
	store := NewStore(filepath.Join(dir, "schedules.json"))
	if err := store.Replace([]Schedule{{ID: "a"}}); err != nil {
		t.Fatalf("replace: %v", err)
	}

	ranAt := time.Date(2026, 8, 26, 8, 0, 0, 0, time.UTC)
	next := time.Date(2026, 8, 27, 8, 0, 0, 0, time.UTC)
	if err := store.RecordRunResult("a", ranAt, "success", "Daily briefing", next); err != nil {
		t.Fatalf("RecordRunResult: %v", err)
	}

	got, _ := store.Get("a")
	if !got.LastRunAt.Equal(ranAt) || got.LastRunStatus != "success" || !got.NextRunAt.Equal(next) {
		t.Errorf("got %+v", got)
	}
}

func TestStore_ReplaceWithTimezonePersistsBoth(t *testing.T) {
	dir := t.TempDir()
	store := NewStore(filepath.Join(dir, "schedules.json"))

	if err := store.ReplaceWithTimezone([]Schedule{{ID: "a"}}, "Asia/Ho_Chi_Minh"); err != nil {
		t.Fatalf("ReplaceWithTimezone: %v", err)
	}

	loc := store.Timezone()
	if loc.String() != "Asia/Ho_Chi_Minh" {
		t.Errorf("Timezone() = %v, want Asia/Ho_Chi_Minh", loc)
	}

	// A plain Replace afterwards must preserve that timezone.
	if err := store.Replace([]Schedule{{ID: "a"}, {ID: "b"}}); err != nil {
		t.Fatalf("replace: %v", err)
	}
	if loc := store.Timezone(); loc.String() != "Asia/Ho_Chi_Minh" {
		t.Errorf("Timezone() after plain Replace = %v, want preserved Asia/Ho_Chi_Minh", loc)
	}
}

func TestStore_TimezoneFallsBackToUTC(t *testing.T) {
	dir := t.TempDir()
	store := NewStore(filepath.Join(dir, "schedules.json")) // never written

	if loc := store.Timezone(); loc != time.UTC {
		t.Errorf("Timezone() on a fresh store = %v, want UTC", loc)
	}

	if err := store.ReplaceWithTimezone(nil, "Not/A/Real/Zone"); err != nil {
		t.Fatalf("ReplaceWithTimezone: %v", err)
	}
	if loc := store.Timezone(); loc != time.UTC {
		t.Errorf("Timezone() with an invalid zone = %v, want UTC fallback", loc)
	}
}

func TestSchedule_NextRunFoldsInEndAtAndJitter(t *testing.T) {
	tz := time.UTC
	endAt := time.Date(2026, 8, 20, 0, 0, 0, 0, tz) // already past
	sch := Schedule{
		ID:      "a",
		Cadence: Spec{Repeat: RepeatDaily, Time: "08:00"},
		EndAt:   &endAt,
	}
	after := time.Date(2026, 8, 26, 7, 0, 0, 0, tz)
	if _, ok := sch.NextRun(after, tz, "device-1"); ok {
		t.Error("Schedule.NextRun must respect the wire's top-level end_at")
	}

	sch.EndAt = nil
	got, ok := sch.NextRun(after, tz, "device-1")
	if !ok {
		t.Fatal("expected ok=true once end_at is cleared")
	}
	base := time.Date(2026, 8, 26, 8, 0, 0, 0, tz)
	want := base.Add(JitterOffset("device-1", "a"))
	if !got.Equal(want) {
		t.Errorf("got %v, want %v (base + this device's jitter)", got, want)
	}
}
