package mqtthandler

import (
	"testing"

	"go.autonomous.ai/os/system/schedule"
)

func confirmedItem(id, name string, rev uint64) scheduleListItem {
	return scheduleListItem{ID: id, Name: name, Rev: rev, Enabled: true}
}

// An unconfirmed create is visible and marked pending.
func TestOverlayPendingIntents_CreateAppearsAsPending(t *testing.T) {
	items := []scheduleListItem{confirmedItem("sched-1", "Existing", 4)}
	intents := []schedule.Intent{{
		IntentID: "int-1",
		Op:       "create",
		Payload:  &schedule.IntentPayload{Name: "Brand new", Instructions: "do it", Enabled: true},
	}}

	got := overlayPendingIntents(items, intents)
	if len(got) != 2 {
		t.Fatalf("got %d rows, want 2", len(got))
	}
	created := got[1]
	if created.Pending != "create" {
		t.Errorf("pending = %q, want create", created.Pending)
	}
	if created.Name != "Brand new" {
		t.Errorf("name = %q", created.Name)
	}
	if created.ID != "intent:int-1" {
		t.Errorf("id = %q, want intent:int-1", created.ID)
	}
	if got[0].Pending != "" {
		t.Errorf("the confirmed row was wrongly marked pending: %q", got[0].Pending)
	}
}

// A pending update shows the proposed values.
func TestOverlayPendingIntents_UpdateShowsProposedValues(t *testing.T) {
	items := []scheduleListItem{confirmedItem("sched-1", "Old name", 4)}
	intents := []schedule.Intent{{
		IntentID:   "int-2",
		Op:         "update",
		ScheduleID: "sched-1",
		BaseRev:    4,
		Payload:    &schedule.IntentPayload{Name: "New name", Instructions: "changed", Enabled: false},
	}}

	got := overlayPendingIntents(items, intents)
	if len(got) != 1 {
		t.Fatalf("update must not add a row, got %d", len(got))
	}
	if got[0].Name != "New name" || got[0].Instructions != "changed" {
		t.Errorf("proposed values not shown: %+v", got[0])
	}
	if got[0].Enabled {
		t.Error("proposed enabled=false not reflected")
	}
	if got[0].Pending != "update" {
		t.Errorf("pending = %q, want update", got[0].Pending)
	}
	// rev stays the CONFIRMED one: it is what the next edit must quote as
	// base_rev, and inventing a higher one locally would guarantee a lost CAS.
	if got[0].Rev != 4 {
		t.Errorf("rev = %d, want the confirmed 4", got[0].Rev)
	}
}

// A pending delete keeps the row.
func TestOverlayPendingIntents_DeleteKeepsRowMarked(t *testing.T) {
	items := []scheduleListItem{confirmedItem("sched-1", "Doomed", 2)}
	intents := []schedule.Intent{{IntentID: "int-3", Op: "delete", ScheduleID: "sched-1", BaseRev: 2}}

	got := overlayPendingIntents(items, intents)
	if len(got) != 1 {
		t.Fatalf("delete must not drop the row locally, got %d", len(got))
	}
	if got[0].Pending != "delete" {
		t.Errorf("pending = %q, want delete", got[0].Pending)
	}
	if got[0].Name != "Doomed" {
		t.Errorf("row content changed: %+v", got[0])
	}
}

// An intent whose target is gone is skipped.
func TestOverlayPendingIntents_IgnoresIntentsForVanishedRows(t *testing.T) {
	items := []scheduleListItem{confirmedItem("sched-1", "Still here", 1)}
	intents := []schedule.Intent{
		{IntentID: "int-4", Op: "update", ScheduleID: "ghost", Payload: &schedule.IntentPayload{Name: "nope"}},
		{IntentID: "int-5", Op: "delete", ScheduleID: "ghost"},
	}

	got := overlayPendingIntents(items, intents)
	if len(got) != 1 {
		t.Fatalf("got %d rows, want 1", len(got))
	}
	if got[0].Pending != "" || got[0].Name != "Still here" {
		t.Errorf("surviving row was disturbed: %+v", got[0])
	}
}

func TestOverlayPendingIntents_EmptyQueueIsIdentity(t *testing.T) {
	items := []scheduleListItem{confirmedItem("a", "A", 1), confirmedItem("b", "B", 2)}
	got := overlayPendingIntents(items, nil)
	if len(got) != 2 || got[0].Pending != "" || got[1].Pending != "" {
		t.Fatalf("empty queue changed the list: %+v", got)
	}
}
