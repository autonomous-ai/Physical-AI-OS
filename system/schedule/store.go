package schedule

import (
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// Task kinds (wire values, do not rename). Empty resolves to KindAgent; see ResolveKind.
const (
	KindAgent = "agent"
	KindSpeak = "speak"
)

// MaxSpeakChars is HAL's /voice/speak max_length; longer text is rejected (422), not truncated.
const MaxSpeakChars = 2000

// MaxTimesPerSchedule mirrors the BFF cap; enforced here for device-authored schedules.
const MaxTimesPerSchedule = 12

// ResolveKind maps a wire kind to the runner kind; empty or unknown values resolve to KindAgent.
func ResolveKind(kind string) string {
	switch strings.TrimSpace(strings.ToLower(kind)) {
	case KindSpeak:
		return KindSpeak
	default:
		return KindAgent
	}
}

// Schedule is one task as sent by schedule.sync, plus device-local run bookkeeping.
type Schedule struct {
	ID           string `json:"id"`
	Name         string `json:"name"`
	Instructions string `json:"instructions"`
	Enabled      bool   `json:"enabled"`

	// Kind is KindAgent (prompt) or KindSpeak (literal TTS text); read via ResolveKind.
	Kind string `json:"kind,omitempty"`

	// Requires lists connector codes the task needs; a missing one makes the run "skipped".
	// Backend-owned: replaced on every sync, never carried forward.
	Requires []string `json:"requires,omitempty"`

	// Cadence is the wire's nested "schedule" object.
	Cadence Spec `json:"schedule"`

	// Rev is the backend revision, quoted back as base_rev on edits; never set by the device.
	Rev uint64 `json:"rev,omitempty"`

	// EndAt is the top-level wire end_at; nil means no expiry.
	EndAt *time.Time `json:"end_at,omitempty"`

	// Local bookkeeping, not on the wire. LastRunStatus is "success" | "failure" | "skipped".
	NextRunAt     time.Time `json:"next_run_at,omitempty"`
	LastRunAt     time.Time `json:"last_run_at,omitempty"`
	LastRunStatus string    `json:"last_run_status,omitempty"`

	// LastRunSummary is the last RunReport.Summary (local; kept across syncs).
	LastRunSummary string `json:"last_run_summary,omitempty"`

	// LastFailedOccurrence is the NextRunAt of the last failed attempt; the runner compares it
	// with the current NextRunAt to ack only the first failure per occurrence. Local only.
	LastFailedOccurrence time.Time `json:"last_failed_occurrence,omitempty"`
}

// NextRun returns when s next fires after `after`, including per-device jitter; false if spent.
func (s Schedule) NextRun(after time.Time, tz *time.Location, deviceID string) (time.Time, bool) {
	spec := s.Cadence
	spec.EndAt = s.EndAt
	return NextRunForDevice(spec, after, tz, deviceID, s.ID)
}

// storeFile is the on-disk shape of schedules.json; Timezone is kept here for restart-safe ticks.
type storeFile struct {
	Timezone  string     `json:"timezone,omitempty"`
	Schedules []Schedule `json:"schedules"`
}

// Store persists schedules to schedules.json, a sibling of config.json (never inside it,
// so writes don't trigger config reloads). All access is serialized by mu.
type Store struct {
	mu   sync.Mutex
	path string
}

// NewStore returns a Store backed by path; nothing is read until first use.
func NewStore(path string) *Store {
	return &Store{path: path}
}

// loadFileLocked reads schedules.json, degrading to empty on any error.
func (s *Store) loadFileLocked() storeFile {
	f, _ := s.readFileLocked()
	return f
}

// readFileLocked parses schedules.json. Missing or corrupt files yield an empty struct;
// only an unreadable existing file returns an error.
func (s *Store) readFileLocked() (storeFile, error) {
	data, err := os.ReadFile(s.path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return storeFile{}, nil
		}
		return storeFile{}, err
	}
	var f storeFile
	if err := json.Unmarshal(data, &f); err != nil {
		return storeFile{}, nil
	}
	return f, nil
}

// saveFileLocked writes a temp file in the same dir then renames it atomically.
func (s *Store) saveFileLocked(f storeFile) error {
	dir := filepath.Dir(s.path)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".schedules.*.tmp")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpPath)
		return err
	}
	if err := os.Rename(tmpPath, s.path); err != nil {
		os.Remove(tmpPath)
		return err
	}
	return nil
}

// Load returns the schedule list; a missing or corrupt file yields an empty list.
func (s *Store) Load() ([]Schedule, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.loadFileLocked().Schedules, nil
}

// LoadChecked is Load but errors when the file exists and cannot be read, so the
// schedules digest is omitted instead of reporting an empty list.
func (s *Store) LoadChecked() ([]Schedule, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	f, err := s.readFileLocked()
	if err != nil {
		return nil, err
	}
	return f.Schedules, nil
}

// carryLocalBookkeeping copies zero-valued local run fields from prior onto next, by id.
// Without it every sync wipes LastFailedOccurrence and re-opens duplicate failure acks.
func carryLocalBookkeeping(next []Schedule, prior []Schedule) []Schedule {
	if len(prior) == 0 || len(next) == 0 {
		return next
	}
	byID := make(map[string]Schedule, len(prior))
	for _, p := range prior {
		byID[p.ID] = p
	}
	for i := range next {
		p, ok := byID[next[i].ID]
		if !ok {
			continue
		}
		if next[i].LastRunAt.IsZero() {
			next[i].LastRunAt = p.LastRunAt
		}
		if next[i].LastRunStatus == "" {
			next[i].LastRunStatus = p.LastRunStatus
		}
		if next[i].LastRunSummary == "" {
			next[i].LastRunSummary = p.LastRunSummary
		}
		if next[i].LastFailedOccurrence.IsZero() {
			next[i].LastFailedOccurrence = p.LastFailedOccurrence
		}
	}
	return next
}

// Replace swaps in a new schedule list, keeping the stored timezone and local bookkeeping.
func (s *Store) Replace(schedules []Schedule) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.loadFileLocked()
	f.Schedules = carryLocalBookkeeping(schedules, f.Schedules)
	return s.saveFileLocked(f)
}

// ReplaceWithTimezone is Replace plus the timezone, in one atomic write.
func (s *Store) ReplaceWithTimezone(schedules []Schedule, timezone string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	prior := s.loadFileLocked().Schedules
	return s.saveFileLocked(storeFile{
		Timezone:  timezone,
		Schedules: carryLocalBookkeeping(schedules, prior),
	})
}

// SetTimezone updates only the device-wide timezone and reports whether it changed.
func (s *Store) SetTimezone(timezone string) (changed bool, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.loadFileLocked()
	if f.Timezone == timezone {
		return false, nil
	}
	f.Timezone = timezone
	return true, s.saveFileLocked(f)
}

// Get returns one schedule by id.
func (s *Store) Get(id string) (Schedule, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, sch := range s.loadFileLocked().Schedules {
		if sch.ID == id {
			return sch, true
		}
	}
	return Schedule{}, false
}

// mutate applies fn to the schedule with id under one lock and saves; unknown id is a no-op.
func (s *Store) mutate(id string, fn func(*Schedule)) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.loadFileLocked()
	for i := range f.Schedules {
		if f.Schedules[i].ID == id {
			fn(&f.Schedules[i])
			return s.saveFileLocked(f)
		}
	}
	return nil
}

// SetLastRun records a run outcome without touching NextRunAt (manual "Run now").
func (s *Store) SetLastRun(id string, at time.Time, status, summary string) error {
	return s.mutate(id, func(sch *Schedule) {
		sch.LastRunAt = at
		sch.LastRunStatus = status
		sch.LastRunSummary = summary
	})
}

// RecordRunResult persists a ticker run outcome and the next occurrence in one atomic write,
// clearing LastFailedOccurrence (the occurrence is resolved).
func (s *Store) RecordRunResult(id string, at time.Time, status, summary string, nextRunAt time.Time) error {
	return s.mutate(id, func(sch *Schedule) {
		sch.LastRunAt = at
		sch.LastRunStatus = status
		sch.LastRunSummary = summary
		sch.NextRunAt = nextRunAt
		sch.LastFailedOccurrence = time.Time{}
	})
}

// SetLastFailedRun records a failed attempt pinned to occurrence; NextRunAt is untouched (I5).
func (s *Store) SetLastFailedRun(id string, at time.Time, summary string, occurrence time.Time) error {
	return s.mutate(id, func(sch *Schedule) {
		sch.LastRunAt = at
		sch.LastRunStatus = "failure"
		sch.LastRunSummary = summary
		sch.LastFailedOccurrence = occurrence
	})
}

// SetNextRun updates only NextRunAt (sync seeding, stale re-anchor).
func (s *Store) SetNextRun(id string, at time.Time) error {
	return s.mutate(id, func(sch *Schedule) {
		sch.NextRunAt = at
	})
}

// Timezone returns the stored IANA zone, falling back to UTC when empty or invalid.
func (s *Store) Timezone() *time.Location {
	s.mu.Lock()
	name := s.loadFileLocked().Timezone
	s.mu.Unlock()
	if name == "" {
		return time.UTC
	}
	loc, err := time.LoadLocation(name)
	if err != nil {
		return time.UTC
	}
	return loc
}
