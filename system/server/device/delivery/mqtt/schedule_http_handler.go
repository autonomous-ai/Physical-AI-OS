package mqtthandler

import (
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/schedule"
	"go.autonomous.ai/os/system/server/serializers"
)

// scheduleListItem is the HTTP read model for one row of the device web UI's
// read-only "Scheduled" section. It is NOT a second copy of the schedule data
// — schedules.json via h.scheduleStore stays the only source of truth — this
// struct only reshapes schedule.Schedule for JSON on the way out of a single
// request:
//
//   - NextRunAt/LastRunAt become *time.Time so a never-fired/never-run zero
//     instant is OMITTED from the payload instead of serializing as Go's zero
//     time "0001-01-01T00:00:00Z" (the web UI must render that as "Never",
//     not a year-1 date — easiest to just not ship it).
//   - Cadence keeps the wire's own "schedule" JSON key so this shape matches
//     schedule.Schedule's on-disk/on-wire layout exactly, just with the
//     bookkeeping timestamps flattened to pointers.
type scheduleListItem struct {
	ID           string `json:"id"`
	Name         string `json:"name"`
	Instructions string `json:"instructions"`
	Enabled      bool   `json:"enabled"`

	// Kind is "agent" or "speak" — WHAT firing this task does, and so how
	// Instructions is read. It must be echoed here, not just accepted on the
	// write endpoints: the web UI seeds its editor from this response, so a
	// missing kind made every stored "speak" task reopen as an "agent" one and
	// silently demote itself on the next save.
	//
	// Written unconditionally rather than with omitempty: ResolveKind always
	// yields a concrete "agent"/"speak", and an absent key would put the
	// client back to inferring a default it cannot see.
	Kind          string        `json:"kind"`
	Cadence       schedule.Spec `json:"schedule"`
	EndAt         *time.Time    `json:"end_at,omitempty"`
	NextRunAt     *time.Time    `json:"next_run_at,omitempty"`
	LastRunAt     *time.Time    `json:"last_run_at,omitempty"`
	LastRunStatus string        `json:"last_run_status,omitempty"`

	// LastRunSummary is schedule.Schedule.LastRunSummary, echoed so the UI
	// can say why a run was skipped: "missing connector: gmail" renders as
	// "Skipped · gmail isn't connected". Omitted for a never-run row.
	LastRunSummary string `json:"last_run_summary,omitempty"`

	// Rev is the backend revision this row is at, echoed so the UI can show
	// staleness and so a client can round-trip it if it ever needs to.
	Rev uint64 `json:"rev,omitempty"`

	// Pending is "" for a confirmed row, or "create" / "update" / "delete"
	// when a locally-made change is still queued. This is how the UI shows an
	// edit immediately while being honest that it has not been confirmed —
	// and, for a create, that the task is NOT yet armed.
	Pending string `json:"pending,omitempty"`

	// IntentID is set only on a pending row, so the UI can correlate it with
	// the queue entry.
	IntentID string `json:"intent_id,omitempty"`
}

// toScheduleListItem reshapes one stored schedule for the HTTP response — see
// scheduleListItem's doc comment for why NextRunAt/LastRunAt become pointers.
func toScheduleListItem(sch schedule.Schedule) scheduleListItem {
	item := scheduleListItem{
		ID:           sch.ID,
		Name:         sch.Name,
		Instructions: sch.Instructions,
		Enabled:      sch.Enabled,
		// Normalised, not raw: a row stored before this field holds "", which
		// the runner already treats as agent — so the UI must be told "agent"
		// rather than left to guess from an empty string.
		Kind:          schedule.ResolveKind(sch.Kind),
		Cadence:       sch.Cadence,
		EndAt:         sch.EndAt,
		LastRunStatus: sch.LastRunStatus,
		Rev:           sch.Rev,

		LastRunSummary: sch.LastRunSummary,
	}
	if !sch.NextRunAt.IsZero() {
		nextRunAt := sch.NextRunAt
		item.NextRunAt = &nextRunAt
	}
	if !sch.LastRunAt.IsZero() {
		lastRunAt := sch.LastRunAt
		item.LastRunAt = &lastRunAt
	}
	return item
}

// ListSchedules handles GET /api/schedule/list for the device web UI's
// read-only "Scheduled" section. A thin JSON reshape over h.scheduleStore's
// existing Load()/Timezone() — the very same Store the MQTT
// schedule.sync/schedule.run handlers and the ticker (Runner) already read —
// so there is no second copy of the schedule data anywhere and no new state.
//
// A device that has never received a schedule.sync has no schedules.json at
// all; Store.Load degrades that to an empty slice rather than an error (see
// loadFileLocked's doc comment), so this handler naturally returns an empty
// "schedules" list in that case — the web UI's empty state ("no schedules
// yet"), not an error page.
func (h *DeviceMQTTHandler) ListSchedules(c *gin.Context) {
	schedules, err := h.scheduleStore.Load()
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	items := make([]scheduleListItem, 0, len(schedules))
	for _, sch := range schedules {
		items = append(items, toScheduleListItem(sch))
	}
	// Overlay whatever the user has changed locally but the backend has not
	// confirmed yet. Without this a create would simply vanish from the UI
	// until the round trip completed, which reads as "it didn't work" —
	// especially on a device that happens to be offline.
	items = overlayPendingIntents(items, h.scheduleIntents.List())
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		// The store's own timezone default (falls back to UTC — see
		// Store.Timezone's doc comment) so the web UI can render next/last
		// run times without guessing the device's zone itself.
		"timezone":  h.scheduleStore.Timezone().String(),
		"schedules": items,
	}))
}

// RunScheduleNow handles POST /api/schedule/:id/run — the web UI's local
// "Run now" button. It fires through the EXACT SAME schedule.Runner.RunNow
// the MQTT schedule.run kind uses (see handleScheduleRun in
// schedule_run_handler.go), so a manual trigger from this admin page acks the
// same fd_channel schedule.run report the backend already knows how to
// consume, regardless of whether the request came from the cloud or from
// here. RunNow only ever updates last-run bookkeeping — cadence and
// next_run_at are left exactly as they were (see Runner.RunNow's doc
// comment) — so this can never perturb the schedule's regular cadence or
// race the ticker's own automatic fire.
func (h *DeviceMQTTHandler) RunScheduleNow(c *gin.Context) {
	id := strings.TrimSpace(c.Param("id"))
	// resolveScheduleRun is the exact lookup schedule_run_handler.go's
	// handleScheduleRun uses for the MQTT path — reused here so "unknown id"
	// can never be defined differently between the two entry points. Its only
	// two failure modes are a blank id (unreachable through Gin's :id-bound
	// routing) and an unknown id, both of which the endpoint contract maps to
	// 404.
	sch, errMsg := resolveScheduleRun(h.scheduleStore, id)
	if errMsg != "" {
		c.JSON(http.StatusNotFound, serializers.ResponseError(errMsg))
		return
	}

	rr, ran := h.scheduleRunner.RunNow(sch)
	if !ran {
		// Deferred, not failed: the agent is mid-turn (single-flight — see
		// Runner.RunNow). No ack was published on this path (RunNow never
		// reached SendSystemChatMessage), so this HTTP response is the only
		// place the caller learns "busy, try again".
		c.JSON(http.StatusConflict, serializers.ResponseError("agent busy, try again shortly"))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"id":         rr.ScheduleID,
		"run_id":     rr.RunID,
		"started_at": rr.StartedAt.Format(time.RFC3339),
		"status":     rr.Status,
		"summary":    rr.Summary,
	}))
}

// overlayPendingIntents merges the unconfirmed intent queue onto the confirmed
// rows for display.
//
// The three ops surface differently because they mean different things to the
// user: a pending CREATE is a row that does not exist server-side yet (and is
// not armed), a pending UPDATE is an existing row shown with its proposed
// values, and a pending DELETE is an existing row still running until the
// backend agrees to remove it. Marking all three the same would be simpler and
// would misrepresent two of them.
//
// Pure function of its inputs so the merge is testable without a store, a
// broker, or a device.
func overlayPendingIntents(items []scheduleListItem, intents []schedule.Intent) []scheduleListItem {
	byID := make(map[string]int, len(items))
	for i, it := range items {
		byID[it.ID] = i
	}

	for _, in := range intents {
		switch in.Op {
		case "create":
			if in.Payload == nil {
				continue
			}
			items = append(items, scheduleListItem{
				// A pending create has no backend id yet — the backend assigns
				// one. Key it by intent so the UI has something stable to
				// render and act on until the real row arrives.
				ID:           "intent:" + in.IntentID,
				Name:         in.Payload.Name,
				Instructions: in.Payload.Instructions,
				// Same reason as toScheduleListItem: without this a pending
				// speak task renders as an agent one until the backend
				// confirms it, so the row the user just created misdescribes
				// itself for as long as it is queued.
				Kind:     schedule.ResolveKind(in.Payload.Kind),
				Enabled:  in.Payload.Enabled,
				Cadence:  in.Payload.Cadence,
				EndAt:    in.Payload.EndAt,
				Pending:  "create",
				IntentID: in.IntentID,
			})
		case "update":
			idx, ok := byID[in.ScheduleID]
			if !ok || in.Payload == nil {
				continue
			}
			items[idx].Name = in.Payload.Name
			items[idx].Instructions = in.Payload.Instructions
			items[idx].Kind = schedule.ResolveKind(in.Payload.Kind)
			items[idx].Enabled = in.Payload.Enabled
			items[idx].Cadence = in.Payload.Cadence
			items[idx].EndAt = in.Payload.EndAt
			items[idx].Pending = "update"
			items[idx].IntentID = in.IntentID
		case "delete":
			idx, ok := byID[in.ScheduleID]
			if !ok {
				continue
			}
			items[idx].Pending = "delete"
			items[idx].IntentID = in.IntentID
		}
	}
	return items
}
