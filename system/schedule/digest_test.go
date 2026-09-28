package schedule

import (
	"os"
	"path/filepath"
	"testing"
)

// The golden vectors from the shared digest spec (firmware Task 18 ⇄ worker
// Task 19). The worker computes the same digest over its lobster.schedules
// rows and forces a full schedule.sync when the two differ, so these values
// are a CROSS-REPO contract: if this test ever needs its expectations
// changed, the worker's copy of the same table must change in lockstep — and
// the "v1:" prefix must be bumped, or every device/backend pair mid-rollout
// will read as drifted and re-sync on every info uplink.
func TestDigest_GoldenVectors(t *testing.T) {
	v1 := []Schedule{
		{ID: "a1", Rev: 3, Requires: []string{"gmail"}},
		{ID: "b2", Rev: 0},
	}
	cases := []struct {
		name string
		rows []Schedule
		want string
	}{
		{"V0 no rows", nil,
			"v1:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
		{"V1", v1,
			"v1:74f9d2edc313294a4b32aa703b46e70cbefae777b977a5b19b254c2ba9044a70"},
		{"V2 V1 in reverse order", []Schedule{v1[1], v1[0]},
			"v1:74f9d2edc313294a4b32aa703b46e70cbefae777b977a5b19b254c2ba9044a70"},
		{"V3", []Schedule{{ID: "x", Rev: 12, Requires: []string{"gmail", "slack"}}},
			"v1:8a854492dbc822b97d9f466832368ea54ae119726e6b6053da72d5d1800a52b3"},
		{"V4 V1 with a1 rev 4", []Schedule{{ID: "a1", Rev: 4, Requires: []string{"gmail"}}, {ID: "b2", Rev: 0}},
			"v1:46c5df036ee57428fb1b395e4e610e61eccf876b52ae29ce7a4348d150f4bad0"},
		{"V0 empty non-nil slice", []Schedule{},
			"v1:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Digest(tc.rows); got != tc.want {
				t.Fatalf("Digest = %s, want %s", got, tc.want)
			}
		})
	}
}

// Only id, rev and requires are hashed — the fields the backend owns and the
// drift check is about. Everything else (enabled, name, run bookkeeping) is
// deliberately outside the digest, so a run or a pause never reads as drift.
func TestDigest_IgnoresFieldsOutsideTheSpec(t *testing.T) {
	base := []Schedule{{ID: "a1", Rev: 3, Requires: []string{"gmail"}}, {ID: "b2"}}
	noisy := []Schedule{
		{ID: "a1", Rev: 3, Requires: []string{"gmail"}, Name: "Inbox digest", Enabled: true, LastRunStatus: "skipped"},
		{ID: "b2", Name: "Paused", Enabled: false, Instructions: "x"},
	}
	if Digest(base) != Digest(noisy) {
		t.Fatal("fields outside id/rev/requires changed the digest")
	}
}

// requires is hashed in STORED order (the order the backend resolved it in),
// not sorted — the spec says so, and the worker hashes its rows the same way.
func TestDigest_RequiresOrderIsSignificant(t *testing.T) {
	a := Digest([]Schedule{{ID: "x", Rev: 12, Requires: []string{"gmail", "slack"}}})
	b := Digest([]Schedule{{ID: "x", Rev: 12, Requires: []string{"slack", "gmail"}}})
	if a == b {
		t.Fatal("requires order did not affect the digest; the spec hashes it in stored order")
	}
}

// Digest sorts a copy: the caller's slice (e.g. the store's rows) must come
// back in the order it went in.
func TestDigest_DoesNotReorderTheCallersSlice(t *testing.T) {
	rows := []Schedule{{ID: "b2"}, {ID: "a1"}}
	_ = Digest(rows)
	if rows[0].ID != "b2" || rows[1].ID != "a1" {
		t.Fatalf("caller's slice reordered: %v", rows)
	}
}

// LoadChecked is Load plus an honest error for a file that exists but cannot
// be READ — the one case where the info uplink must omit the digest rather
// than claim "no schedules".
func TestStore_LoadChecked(t *testing.T) {
	t.Run("missing file is a legitimately empty store", func(t *testing.T) {
		rows, err := NewStore(filepath.Join(t.TempDir(), "schedules.json")).LoadChecked()
		if err != nil || len(rows) != 0 {
			t.Fatalf("LoadChecked = (%v, %v), want (empty, nil)", rows, err)
		}
	})
	t.Run("rows are returned as stored", func(t *testing.T) {
		s := newTestStore(t)
		if err := s.Replace([]Schedule{{ID: "a1", Rev: 3, Requires: []string{"gmail"}}}); err != nil {
			t.Fatal(err)
		}
		rows, err := s.LoadChecked()
		if err != nil || len(rows) != 1 || rows[0].ID != "a1" || rows[0].Rev != 3 {
			t.Fatalf("LoadChecked = (%+v, %v)", rows, err)
		}
	})
	t.Run("corrupt file reads as empty, exactly like the runner's view", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "schedules.json")
		if err := os.WriteFile(path, []byte("{truncated"), 0o600); err != nil {
			t.Fatal(err)
		}
		rows, err := NewStore(path).LoadChecked()
		if err != nil || len(rows) != 0 {
			t.Fatalf("LoadChecked = (%v, %v), want (empty, nil)", rows, err)
		}
	})
	t.Run("unreadable file is an error", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "schedules.json")
		if err := os.Mkdir(path, 0o700); err != nil { // a directory: ReadFile fails
			t.Fatal(err)
		}
		if _, err := NewStore(path).LoadChecked(); err == nil {
			t.Fatal("LoadChecked returned no error for an unreadable schedules.json")
		}
	})
}
