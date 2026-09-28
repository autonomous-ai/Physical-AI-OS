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

// Task kinds — exact strings per the MQTT schedule.sync wire contract. Do not
// rename: the backend, the web app and this device all send/expect them
// verbatim.
//
// KindAgent is the ONLY behaviour that existed before this field, which is why
// the empty string must resolve to it — see Schedule.Kind and ResolveKind.
const (
	KindAgent = "agent"
	KindSpeak = "speak"
)

// MaxSpeakChars is HAL's hard TTS limit, mirrored here so a locally authored
// speak task is refused at the form instead of at 8am every morning.
//
// The number is not ours to choose: hal/models.py declares the /voice/speak
// text field as Field(..., min_length=1, max_length=2000). A longer string is
// rejected with a 422 and is NOT truncated, so the device says nothing at all
// — a silent, repeating, once-a-day failure that looks like the feature is
// broken rather than like the text is too long. The backend enforces the same
// bound on create/update; this is the second line of the same defence, and the
// one that can report the problem while the user is still looking at the form.
const MaxSpeakChars = 2000

// MaxTimesPerSchedule mirrors the BFF's cap of the same name. The device is
// the last line of defence for a device-AUTHORED schedule, which never passes
// through the BFF's validation before being stored locally.
const MaxTimesPerSchedule = 12

// ResolveKind maps a raw wire value onto the kind the runner should actually
// use. Empty resolves to KindAgent because every schedule stored before the
// field existed carries "", and each of them must keep behaving exactly as it
// did. An UNRECOGNISED value resolves to KindAgent too, deliberately: a device
// running older firmware than the backend must degrade to the long-standing
// behaviour rather than refuse to run the task at all, since silently never
// firing is the worse failure for something the user scheduled.
func ResolveKind(kind string) string {
	switch strings.TrimSpace(strings.ToLower(kind)) {
	case KindSpeak:
		return KindSpeak
	default:
		return KindAgent
	}
}

// Schedule is one recurring (or one-shot) task, exactly as the backend's
// schedule.sync payload describes it, plus a handful of store-only bookkeeping
// fields the device needs to run the scheduler across restarts.
type Schedule struct {
	ID           string `json:"id"`
	Name         string `json:"name"`
	Instructions string `json:"instructions"`
	Enabled      bool   `json:"enabled"`

	// Kind selects WHAT firing this schedule does, and so how Instructions is
	// read: KindAgent ("agent") hands it to the agent runtime as a prompt;
	// KindSpeak ("speak") treats it as the literal words to say and posts them
	// straight to TTS, with no agent turn at all.
	//
	// Read through ResolveKind, never directly — "" (every row written before
	// this field existed) and any value this firmware does not recognise both
	// mean "agent". omitempty keeps an agent task's on-disk JSON byte-identical
	// to what it was before this field, so nothing about existing devices
	// changes on upgrade.
	Kind string `json:"kind,omitempty"`

	// Requires lists the connector codes (catalog codes, exactly as in
	// connector.set.<code>) this task cannot do its job without — e.g.
	// ["gmail"] for an inbox digest. The backend resolves it once, when the
	// task is created from a template, and ships it on schedule.sync; a custom
	// task, a template with no hard dependency, and every row that predates
	// the field carry none.
	//
	// At fire time the Runner checks each code against what is actually
	// installed on THIS device (see ConnectorChecker) and, when any is
	// missing, skips the run instead of handing the agent a task it cannot
	// complete — reported as status "skipped", see Runner.fire.
	//
	// Backend-owned wire data, like Kind: a schedule.sync replaces it wholesale
	// and carryLocalBookkeeping deliberately does NOT carry it forward, so a
	// requirement dropped upstream stops applying on the very next sync.
	// omitempty keeps a row without requirements byte-identical on disk to
	// what it was before this field existed. Old firmware ignores the key and
	// runs the task as it always did.
	Requires []string `json:"requires,omitempty"`

	// Cadence is the wire's nested "schedule" object. Named Cadence (not
	// Schedule) on the Go side only to avoid a Schedule.Schedule stutter — the
	// JSON tag still matches the wire contract exactly.
	Cadence Spec `json:"schedule"`

	// Rev is the backend-assigned revision of this row, carried straight
	// through from schedule.sync. The device never invents or increments it —
	// it only quotes it back as base_rev when proposing an edit, so the backend
	// can compare-and-swap. A row the device has never synced has rev 0, which
	// no backend row ever has (they start at 1), so a stale proposal is
	// naturally rejected rather than silently applied.
	Rev uint64 `json:"rev,omitempty"`

	// EndAt is a sibling of "schedule" on the wire, not nested inside it (see
	// Spec's doc comment). nil means "no expiry".
	EndAt *time.Time `json:"end_at,omitempty"`

	// NextRunAt, LastRunAt and LastRunStatus are NOT part of the backend wire
	// contract — they are local bookkeeping the runner needs to survive a
	// device restart without re-deriving everything from scratch. Persisted in
	// the same schedules.json (never config.json — see package doc in
	// runner.go) because they change together with the schedule they belong to.
	// LastRunStatus is "success" | "failure" | "skipped" (RunStatusSkipped).
	NextRunAt     time.Time `json:"next_run_at,omitempty"`
	LastRunAt     time.Time `json:"last_run_at,omitempty"`
	LastRunStatus string    `json:"last_run_status,omitempty"`

	// LastRunSummary is the last run's RunReport.Summary, recorded in the same
	// write as LastRunStatus: the task name on success, the error text on
	// failure, "missing connector: <codes>" when skipped. It exists so the
	// device's own Settings page can say WHY a run was skipped ("Skipped ·
	// gmail isn't connected") — the reason would otherwise reach only the
	// backend's ack, and a skip recorded by the ticker would render as a bare
	// status with nothing actionable. Device-local bookkeeping like the fields
	// above (it mirrors the backend's own last_run_summary, but the wire never
	// carries it here), so carryLocalBookkeeping keeps it across a sync.
	LastRunSummary string `json:"last_run_summary,omitempty"`

	// LastFailedOccurrence pins a recorded failure to the SPECIFIC due slot
	// (the schedule's NextRunAt at the time of that failed attempt) it
	// belongs to. Purely a runner concern (see fire()'s ack-suppression
	// logic): while an occurrence is being retried, NextRunAt stays fixed
	// (I5 — a failure must not burn it), so comparing this field against the
	// CURRENT NextRunAt is how the runner tells "this failure is a retry of
	// the occurrence already ack'd" from "this is the first failure of a
	// fresh occurrence" (the previous occurrence's NextRunAt is never equal
	// to the new one, so the comparison naturally resets once the schedule
	// advances or is re-anchored — nothing needs to explicitly clear this on
	// those paths). Never part of the wire contract.
	LastFailedOccurrence time.Time `json:"last_failed_occurrence,omitempty"`
}

// NextRun computes when s next fires, folding in this device's deterministic
// per-schedule jitter (daily/weekly/monthly only — see JitterOffset) so a
// fleet of devices sharing the same cadence doesn't wake in the same instant.
// deviceID is passed in explicitly rather than read from config/global state,
// so this stays reachable from anywhere NextRun is needed (runner ticks, the
// schedule.sync ack, tests) without threading a config dependency into this
// package.
func (s Schedule) NextRun(after time.Time, tz *time.Location, deviceID string) (time.Time, bool) {
	spec := s.Cadence
	spec.EndAt = s.EndAt // fold in the wire's top-level end_at before delegating
	return NextRunForDevice(spec, after, tz, deviceID, s.ID)
}

// storeFile is the on-disk shape of schedules.json. Timezone travels alongside
// the schedules (rather than living only in config.json — see the CRITICAL
// STORAGE RULE in the phase-5 brief) because every wall-clock cadence
// (daily/weekly/monthly) needs it on every runner tick, not just at sync time;
// keeping it here means the runner survives a device restart without waiting
// for a fresh schedule.sync.
type storeFile struct {
	Timezone  string     `json:"timezone,omitempty"`
	Schedules []Schedule `json:"schedules"`
}

// Store persists the device's schedule list to a JSON file (schedules.json,
// a SIBLING of config.json — never inside it: config_watch.go watches
// config.json for changes and reloads a good deal of device state on every
// edit, which a once-a-minute-or-more schedule write must not trigger).
//
// All access is serialized behind mu: the MQTT dispatch goroutine
// (schedule.sync / schedule.run) and the runner's own ticker goroutine both
// touch this concurrently.
type Store struct {
	mu   sync.Mutex
	path string
}

// NewStore returns a Store backed by path. Construction never touches disk —
// even a path whose directory doesn't exist yet is fine until the first write.
func NewStore(path string) *Store {
	return &Store{path: path}
}

// loadFileLocked reads schedules.json. A missing file (first boot — the
// backend hasn't pushed a schedule.sync yet) degrades to an empty struct, not
// an error. A corrupt/truncated file (e.g. a power loss that hit a WRITE
// somehow not covered by the tmp+rename below, or a hand-edited file) degrades
// the same way rather than panicking — the next schedule.sync from the
// backend is authoritative and will repair it. So does a file that exists but
// cannot be read at all; only LoadChecked tells that case apart.
func (s *Store) loadFileLocked() storeFile {
	f, _ := s.readFileLocked()
	return f
}

// readFileLocked is loadFileLocked with the one distinction it hides: an
// error is returned ONLY when the file exists but could not be read (an I/O
// or permission failure). A missing file and a corrupt one still degrade to an
// empty struct with no error — both mean "no usable schedules", which is
// exactly what the runner sees. Keeping the single parse path here means
// LoadChecked can never disagree with Load about what the rows are.
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

// saveFileLocked writes via a temp file in the SAME directory followed by an
// atomic rename, so a crash mid-write can never leave schedules.json
// truncated — a reader always sees either the complete old file or the
// complete new one, never a half-written one.
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

// Load returns the current schedule list. Never errors on a missing or
// corrupt file (see loadFileLocked) — both degrade to an empty list so a
// startup race or a half-crashed write can never crash-loop the device; the
// error return exists for a future on-disk format that can fail harder.
func (s *Store) Load() ([]Schedule, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.loadFileLocked().Schedules, nil
}

// LoadChecked is Load for a caller that must not mistake "could not read the
// file" for "there are no schedules": the info uplink's schedules digest.
// Reporting the empty-list digest for a store that merely failed to read
// would tell the backend the device holds nothing, and it would re-send a
// schedule.sync that the same broken store could not apply either — on every
// info uplink, indefinitely. So an unreadable file is an error here, and the
// caller omits the digest instead.
//
// A missing file and a corrupt one are NOT errors: they return the same empty
// list Load (and so the runner) sees. There the empty digest is the truth,
// and the re-sync it provokes is precisely the repair those cases need — a
// factory reset or a lost schedules.json is what the digest exists to catch.
func (s *Store) LoadChecked() ([]Schedule, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	f, err := s.readFileLocked()
	if err != nil {
		return nil, err
	}
	return f.Schedules, nil
}

// carryLocalBookkeeping copies the DEVICE-LOCAL run fields of prior onto next
// for schedules that survive a replace, keyed by id.
//
// WHY THIS EXISTS: a schedule.sync is a full-state replace built from the wire,
// and the wire carries only what the backend owns — id, name, instructions,
// enabled, schedule, end_at, rev. LastRunAt, LastRunStatus, LastRunSummary and
// LastFailedOccurrence are local bookkeeping that has NO wire representation
// (see their doc comments on Schedule), so they arrive zeroed. Overwriting the
// on-disk values with those zeros loses real state on every single sync.
//
// The cosmetic half is that the device's own Settings page reports "Last run:
// Never" for a task that has run. The half that actually bites is
// LastFailedOccurrence: it is the memory Runner.fire uses to suppress repeated
// failure acks for one occurrence. Zero it, and the next failure inside that
// occurrence's retry window is ack'd again — which is precisely the duplicate
// schedule_run rows that suppression was added to prevent. Any sync landing
// during a retry window re-opens it, and device-originated CRUD makes syncs
// frequent.
//
// Only fields the incoming schedule leaves ZERO are filled in, so a caller that
// deliberately supplies these (tests, or any future path that legitimately
// knows better) still wins. Ids absent from prior — a genuinely new schedule —
// simply keep their zero values.
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

// Replace swaps in a brand-new schedule list, preserving whatever timezone is
// already on disk, and carrying forward the device-local run bookkeeping of any
// schedule that survives the swap (see carryLocalBookkeeping). The schedule.sync
// handler uses ReplaceWithTimezone instead, since it always has both together;
// Replace is for callers (and tests) that only care about the schedule list.
func (s *Store) Replace(schedules []Schedule) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.loadFileLocked()
	f.Schedules = carryLocalBookkeeping(schedules, f.Schedules)
	return s.saveFileLocked(f)
}

// ReplaceWithTimezone is Replace plus the device-wide timezone, written in the
// SAME atomic file write so a crash between "save the schedules" and "save the
// timezone" can never leave a mismatched pair on disk.
func (s *Store) ReplaceWithTimezone(schedules []Schedule, timezone string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	prior := s.loadFileLocked().Schedules
	return s.saveFileLocked(storeFile{
		Timezone:  timezone,
		Schedules: carryLocalBookkeeping(schedules, prior),
	})
}

// SetTimezone updates ONLY the device-wide timezone, leaving the schedule list
// untouched.
//
// Exists because a timezone change arrives on its own downlink (timezone.set),
// not as part of a schedule.sync — and the runner resolves every wall-clock
// cadence against Store.Timezone(). Without this the store keeps whatever
// timezone the last sync happened to carry, so a device moved from UTC to
// Asia/Saigon goes on firing on the old zone until some unrelated edit
// triggers a sync. Observed live: a task set for 11:00 fired at 18:02 local.
//
// Returns whether the value actually changed, so callers can skip the
// next-run recompute when a timezone.set is a no-op repeat.
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

// mutate loads the file, applies fn to the schedule matching id, and saves —
// all under one lock, so the read-modify-write is atomic with respect to other
// Store callers. A no-longer-existing id (e.g. a concurrent schedule.sync
// deleted it) is a silent no-op rather than an error: there is nothing left to
// update, and that's fine.
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

// SetLastRun records the outcome of a run WITHOUT touching NextRunAt. This is
// what the manual "Run now" path (schedule.run) uses — the wire contract is
// explicit that running now must not perturb the schedule's regular cadence.
// The ticker's own automatic fires use RecordRunResult instead, which updates
// last-run bookkeeping AND the next occurrence together in one write.
func (s *Store) SetLastRun(id string, at time.Time, status, summary string) error {
	return s.mutate(id, func(sch *Schedule) {
		sch.LastRunAt = at
		sch.LastRunStatus = status
		sch.LastRunSummary = summary
	})
}

// RecordRunResult persists a scheduled (ticker-driven) run's outcome AND the
// freshly computed next occurrence in ONE atomic write. Doing both together
// matters for crash-safety: two separate writes could leave a schedule that
// looks "already ran" but is still due (fires again on the next tick) or vice
// versa (looks not-yet-run but its due time has already moved on) if the
// device lost power between them.
//
// Called by the runner with a SUCCESS or a SKIPPED outcome — both consume the
// occurrence (a failure uses SetLastFailedRun instead, which deliberately
// leaves NextRunAt alone — see its doc comment), so LastFailedOccurrence is
// cleared here: the occurrence that failure marker pointed at is now resolved.
func (s *Store) RecordRunResult(id string, at time.Time, status, summary string, nextRunAt time.Time) error {
	return s.mutate(id, func(sch *Schedule) {
		sch.LastRunAt = at
		sch.LastRunStatus = status
		sch.LastRunSummary = summary
		sch.NextRunAt = nextRunAt
		sch.LastFailedOccurrence = time.Time{}
	})
}

// SetLastFailedRun records a FAILED attempt, pinning it to occurrence (the
// schedule's NextRunAt at the time of the attempt) via LastFailedOccurrence.
// Like SetLastRun, it never touches NextRunAt — a failure must not burn the
// occurrence (I5). Used only by the runner's automatic ticker path, which
// needs the occurrence pin to suppress a repeated failure ack for retries of
// the SAME occurrence (see runner.fire()); the manual "Run now" path keeps
// using plain SetLastRun since it has no such retry/suppression concept.
func (s *Store) SetLastFailedRun(id string, at time.Time, summary string, occurrence time.Time) error {
	return s.mutate(id, func(sch *Schedule) {
		sch.LastRunAt = at
		sch.LastRunStatus = "failure"
		sch.LastRunSummary = summary
		sch.LastFailedOccurrence = occurrence
	})
}

// SetNextRun updates only NextRunAt. Used by schedule.sync (seeding the
// freshly computed next run for every schedule right after a replace) and by
// the runner's stale-catch-up re-anchor (skipping a missed run forward without
// recording it as having actually run).
func (s *Store) SetNextRun(id string, at time.Time) error {
	return s.mutate(id, func(sch *Schedule) {
		sch.NextRunAt = at
	})
}

// Timezone returns the device-wide IANA zone last set by schedule.sync,
// resolved to a *time.Location. An empty, missing, or unresolvable zone falls
// back to UTC so a bad/absent value degrades to a consistent (if unlocalized)
// schedule instead of crashing the runner.
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
