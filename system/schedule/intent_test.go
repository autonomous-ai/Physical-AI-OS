package schedule

import (
	"path/filepath"
	"testing"
	"time"
)

func tempIntentStore(t *testing.T) *IntentStore {
	t.Helper()
	return NewIntentStore(filepath.Join(t.TempDir(), "schedule-intents.json"))
}

// A missing queue file is an empty queue, not an error.
func TestIntentStore_MissingFileIsEmpty(t *testing.T) {
	if got := tempIntentStore(t).List(); len(got) != 0 {
		t.Fatalf("List() = %d intents, want 0", len(got))
	}
}

func TestIntentStore_AppendListRemoveRoundTrip(t *testing.T) {
	s := tempIntentStore(t)

	a := Intent{IntentID: "a", Op: "create", CreatedAt: time.Now()}
	b := Intent{IntentID: "b", Op: "delete", ScheduleID: "sched-1", BaseRev: 3, CreatedAt: time.Now()}
	if err := s.Append(a); err != nil {
		t.Fatalf("append a: %v", err)
	}
	if err := s.Append(b); err != nil {
		t.Fatalf("append b: %v", err)
	}

	got := s.List()
	if len(got) != 2 {
		t.Fatalf("List() = %d, want 2", len(got))
	}
	if got[0].IntentID != "a" || got[1].IntentID != "b" {
		t.Errorf("order = %s,%s want a,b", got[0].IntentID, got[1].IntentID)
	}
	if got[1].BaseRev != 3 {
		t.Errorf("base_rev = %d, want 3", got[1].BaseRev)
	}

	if err := s.Remove("a"); err != nil {
		t.Fatalf("remove: %v", err)
	}
	got = s.List()
	if len(got) != 1 || got[0].IntentID != "b" {
		t.Fatalf("after remove got %+v, want only b", got)
	}
}

// The queue must survive a new IntentStore instance (reboot).
func TestIntentStore_PersistsAcrossInstances(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "schedule-intents.json")

	first := NewIntentStore(path)
	if err := first.Append(Intent{IntentID: "survives", Op: "create"}); err != nil {
		t.Fatalf("append: %v", err)
	}

	second := NewIntentStore(path)
	got := second.List()
	if len(got) != 1 || got[0].IntentID != "survives" {
		t.Fatalf("reopened store lost the queue: %+v", got)
	}
}

func TestIntentStore_MarkSentStampsAttempts(t *testing.T) {
	s := tempIntentStore(t)
	if err := s.Append(Intent{IntentID: "x", Op: "create"}); err != nil {
		t.Fatalf("append: %v", err)
	}
	at := time.Now()
	if err := s.MarkSent("x", at); err != nil {
		t.Fatalf("mark sent: %v", err)
	}
	if err := s.MarkSent("x", at); err != nil {
		t.Fatalf("mark sent 2: %v", err)
	}
	got := s.List()[0]
	if got.Attempts != 2 {
		t.Errorf("attempts = %d, want 2", got.Attempts)
	}
	if got.LastSentAt.IsZero() {
		t.Error("last_sent_at not stamped")
	}
}

// Each call yields a unique hex key.
func TestNewIntentID_IsUniqueAndHex(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 100; i++ {
		id, err := NewIntentID()
		if err != nil {
			t.Fatalf("generate: %v", err)
		}
		if len(id) != 32 {
			t.Fatalf("id %q length %d, want 32 hex chars", id, len(id))
		}
		if seen[id] {
			t.Fatalf("duplicate intent id %q", id)
		}
		seen[id] = true
	}
}

func TestValidateSpec_AcceptsEachRepeatKind(t *testing.T) {
	at := time.Now().Add(time.Hour)
	for _, tc := range []struct {
		name string
		spec Spec
	}{
		{"daily", Spec{Repeat: "daily", Time: "08:00"}},
		{"weekly", Spec{Repeat: "weekly", Time: "08:00", Days: []int{1, 5}}},
		{"monthly", Spec{Repeat: "monthly", Time: "09:00", DayOfMonth: 15}},
		{"interval", Spec{Repeat: "interval", EveryMs: uint64(15 * time.Minute / time.Millisecond)}},
		{"once", Spec{Repeat: "once", At: &at}},
		{"manual", Spec{Repeat: "manual"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if err := ValidateSpec(tc.spec); err != nil {
				t.Fatalf("ValidateSpec(%s) = %v, want nil", tc.name, err)
			}
		})
	}
}

// Each cadence here would be stored but never fire.
func TestValidateSpec_RejectsUnrunnableCadences(t *testing.T) {
	for _, tc := range []struct {
		name string
		spec Spec
	}{
		{"unknown repeat", Spec{Repeat: "fortnightly", Time: "08:00"}},
		{"daily without time", Spec{Repeat: "daily"}},
		{"daily with bad time", Spec{Repeat: "daily", Time: "25:99"}},
		{"weekly without days", Spec{Repeat: "weekly", Time: "08:00"}},
		{"weekly with out-of-range day", Spec{Repeat: "weekly", Time: "08:00", Days: []int{9}}},
		{"monthly with day 0", Spec{Repeat: "monthly", Time: "08:00", DayOfMonth: 0}},
		{"monthly with day 32", Spec{Repeat: "monthly", Time: "08:00", DayOfMonth: 32}},
		{"interval with no gap", Spec{Repeat: "interval"}},
		{"interval below the floor", Spec{Repeat: "interval", EveryMs: 1000}},
		{"once without a time", Spec{Repeat: "once"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if err := ValidateSpec(tc.spec); err == nil {
				t.Fatalf("ValidateSpec(%s) = nil, want an error", tc.name)
			}
		})
	}
}

func TestValidateIntentPayload_RequiresNameAndInstructions(t *testing.T) {
	ok := Spec{Repeat: "daily", Time: "08:00"}
	for _, tc := range []struct {
		name string
		p    *IntentPayload
	}{
		{"nil", nil},
		{"blank name", &IntentPayload{Name: "  ", Instructions: "do it", Cadence: ok}},
		{"blank instructions", &IntentPayload{Name: "Task", Instructions: "", Cadence: ok}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if err := ValidateIntentPayload(tc.p); err == nil {
				t.Fatal("want an error, got nil")
			}
		})
	}
	if err := ValidateIntentPayload(&IntentPayload{Name: "Task", Instructions: "do it", Cadence: ok}); err != nil {
		t.Fatalf("valid payload rejected: %v", err)
	}
}
