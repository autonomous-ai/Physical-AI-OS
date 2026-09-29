package mqtthandler

import (
	"testing"

	"go.autonomous.ai/os/system/schedule"
)

func TestToScheduleListItem_EchoesKind(t *testing.T) {
	for _, tc := range []struct {
		name  string
		store string
		want  string
	}{
		{"speak survives the round trip", schedule.KindSpeak, schedule.KindSpeak},
		{"agent stays agent", schedule.KindAgent, schedule.KindAgent},
		// The runner already treats that as agent, so the UI must be told
		// "agent" outright rather than left to infer it from a missing key.
		{"empty normalises to agent", "", schedule.KindAgent},
		{"unrecognised normalises to agent", "yodel", schedule.KindAgent},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := toScheduleListItem(schedule.Schedule{ID: "s1", Kind: tc.store})
			if got.Kind != tc.want {
				t.Fatalf("Kind = %q, want %q", got.Kind, tc.want)
			}
		})
	}
}

func TestOverlayPendingIntents_EchoesKind(t *testing.T) {
	t.Run("pending create keeps its kind", func(t *testing.T) {
		items := overlayPendingIntents(nil, []schedule.Intent{{
			IntentID: "i1",
			Op:       "create",
			Payload:  &schedule.IntentPayload{Name: "Water", Kind: schedule.KindSpeak},
		}})
		if len(items) != 1 {
			t.Fatalf("got %d items, want 1", len(items))
		}
		if items[0].Kind != schedule.KindSpeak {
			t.Fatalf("Kind = %q, want %q", items[0].Kind, schedule.KindSpeak)
		}
	})

	t.Run("pending update keeps its kind", func(t *testing.T) {
		items := overlayPendingIntents(
			[]scheduleListItem{{ID: "s1", Kind: schedule.KindAgent}},
			[]schedule.Intent{{
				IntentID:   "i2",
				Op:         "update",
				ScheduleID: "s1",
				Payload:    &schedule.IntentPayload{Name: "Water", Kind: schedule.KindSpeak},
			}},
		)
		if items[0].Kind != schedule.KindSpeak {
			t.Fatalf("Kind = %q, want %q", items[0].Kind, schedule.KindSpeak)
		}
	})
}
