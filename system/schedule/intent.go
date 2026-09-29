package schedule

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

// Intent is one device-originated schedule change, held until the backend confirms it.
// Kept out of schedules.json so the runner never fires an unconfirmed task.
type Intent struct {
	// IntentID is the idempotency key, generated once per user action and kept across retries.
	IntentID string `json:"intent_id"`

	Op         string `json:"op"`                    // "create" | "update" | "delete"
	ScheduleID string `json:"schedule_id,omitempty"` // Target; empty for create

	// BaseRev is the rev the edit was based on (backend compare-and-swap); unused for create.
	BaseRev uint64 `json:"base_rev,omitempty"`

	// Payload is the proposed row for create/update; nil for delete.
	Payload *IntentPayload `json:"schedule,omitempty"`

	CreatedAt  time.Time `json:"created_at"`
	LastSentAt time.Time `json:"last_sent_at,omitempty"`
	Attempts   int       `json:"attempts,omitempty"`
}

// IntentPayload is the user-editable subset of a schedule; backend-owned fields are absent.
type IntentPayload struct {
	Name         string `json:"name"`
	Instructions string `json:"instructions"`
	Enabled      bool   `json:"enabled"`

	// Kind must be carried on every update: the backend writes every field, so omitting it
	// would demote a speak task to an agent task.
	Kind string `json:"kind,omitempty"`

	Timezone     string     `json:"timezone,omitempty"`
	TemplateCode string     `json:"template_code,omitempty"`
	Cadence      Spec       `json:"schedule"`
	EndAt        *time.Time `json:"end_at,omitempty"`
}

// NewIntentID returns a random 128-bit hex idempotency key.
func NewIntentID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", fmt.Errorf("generate intent id: %w", err)
	}
	return hex.EncodeToString(b[:]), nil
}

// intentFile is the on-disk shape of intents.json.
type intentFile struct {
	Intents []Intent `json:"intents"`
}

// IntentStore persists pending intents to intents.json (atomic writes; unreadable file = empty queue).
type IntentStore struct {
	mu   sync.Mutex
	path string
}

func NewIntentStore(path string) *IntentStore {
	return &IntentStore{path: path}
}

func (s *IntentStore) loadLocked() intentFile {
	var f intentFile
	raw, err := os.ReadFile(s.path)
	if err != nil {
		return intentFile{}
	}
	if err := json.Unmarshal(raw, &f); err != nil {
		return intentFile{}
	}
	return f
}

func (s *IntentStore) saveLocked(f intentFile) error {
	if f.Intents == nil {
		f.Intents = []Intent{}
	}
	raw, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal intents: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
		return fmt.Errorf("create intents dir: %w", err)
	}
	tmp, err := os.CreateTemp(filepath.Dir(s.path), ".intents-*.tmp")
	if err != nil {
		return fmt.Errorf("create temp intents: %w", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)

	if _, err := tmp.Write(raw); err != nil {
		tmp.Close()
		return fmt.Errorf("write temp intents: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return fmt.Errorf("sync temp intents: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp intents: %w", err)
	}
	if err := os.Chmod(tmpName, 0o600); err != nil {
		return fmt.Errorf("chmod temp intents: %w", err)
	}
	if err := os.Rename(tmpName, s.path); err != nil {
		return fmt.Errorf("rename intents: %w", err)
	}
	return nil
}

// Append queues one intent.
func (s *IntentStore) Append(in Intent) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.loadLocked()
	f.Intents = append(f.Intents, in)
	return s.saveLocked(f)
}

// List returns the queue in submission order.
func (s *IntentStore) List() []Intent {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.loadLocked()
	out := make([]Intent, len(f.Intents))
	copy(out, f.Intents)
	return out
}

// Remove drops one intent by id once the backend reaches a terminal verdict (applied or rejected).
func (s *IntentStore) Remove(intentID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.loadLocked()
	kept := f.Intents[:0]
	for _, in := range f.Intents {
		if in.IntentID != intentID {
			kept = append(kept, in)
		}
	}
	f.Intents = kept
	return s.saveLocked(f)
}

// MarkSent stamps a send attempt and increments Attempts.
func (s *IntentStore) MarkSent(intentID string, at time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	f := s.loadLocked()
	for i := range f.Intents {
		if f.Intents[i].IntentID == intentID {
			f.Intents[i].LastSentAt = at
			f.Intents[i].Attempts++
			return s.saveLocked(f)
		}
	}
	return nil
}

// ValidateIntentPayload checks a locally-authored schedule before it is queued.
func ValidateIntentPayload(p *IntentPayload) error {
	if p == nil {
		return fmt.Errorf("schedule is required")
	}
	if strings.TrimSpace(p.Name) == "" {
		return fmt.Errorf("name is required")
	}
	if strings.TrimSpace(p.Instructions) == "" {
		return fmt.Errorf("instructions are required")
	}
	if ResolveKind(p.Kind) == KindSpeak {
		// HAL rejects (does not truncate) over-long text; count runes, not bytes.
		if n := utf8.RuneCountInString(p.Instructions); n > MaxSpeakChars {
			return fmt.Errorf("spoken text must be at most %d characters, got %d", MaxSpeakChars, n)
		}
	}
	return ValidateSpec(p.Cadence)
}

// validateClockTimes checks every effective time a spec fires at (count and HH:MM form).
func validateClockTimes(spec Spec) error {
	times := spec.effectiveTimes()
	if len(times) == 0 {
		return fmt.Errorf("schedules need a time")
	}
	if len(times) > MaxTimesPerSchedule {
		return fmt.Errorf("a schedule may have at most %d times, got %d", MaxTimesPerSchedule, len(times))
	}
	for _, t := range times {
		if err := validateClockTime(t); err != nil {
			return err
		}
	}
	return nil
}

// ValidateSpec checks that the fields required by spec.Repeat are present and in range.
func ValidateSpec(spec Spec) error {
	switch spec.Repeat {
	case "daily":
		return validateClockTimes(spec)
	case "weekly":
		if err := validateClockTimes(spec); err != nil {
			return err
		}
		if len(spec.Days) == 0 {
			return fmt.Errorf("weekly schedules need at least one day")
		}
		for _, d := range spec.Days {
			if d < 0 || d > 7 {
				return fmt.Errorf("day of week out of range: %d", d)
			}
		}
		return nil
	case "monthly":
		if err := validateClockTimes(spec); err != nil {
			return err
		}
		if spec.DayOfMonth < 1 || spec.DayOfMonth > 31 {
			return fmt.Errorf("day of month must be 1-31, got %d", spec.DayOfMonth)
		}
		return nil
	case "interval":
		if spec.EveryMs <= 0 {
			return fmt.Errorf("interval schedules need every_ms")
		}
		if time.Duration(spec.EveryMs)*time.Millisecond < minInterval {
			return fmt.Errorf("interval must be at least %s", minInterval)
		}
		return nil
	case "once":
		if spec.At == nil || spec.At.IsZero() {
			return fmt.Errorf("one-off schedules need a date and time")
		}
		return nil
	case "manual":
		return nil
	default:
		return fmt.Errorf("unsupported repeat: %q", spec.Repeat)
	}
}

// validateClockTime accepts exactly the "HH:MM" 24-hour form the runner parses.
func validateClockTime(s string) error {
	if _, err := time.Parse("15:04", s); err != nil {
		return fmt.Errorf("time must be HH:MM, got %q", s)
	}
	return nil
}
