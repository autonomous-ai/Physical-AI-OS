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
// read-only "Scheduled" section.
type scheduleListItem struct {
	ID           string `json:"id"`
	Name         string `json:"name"`
	Instructions string `json:"instructions"`
	Enabled      bool   `json:"enabled"`

	// Kind is "agent" or "speak" — WHAT firing this task does, and so how
	// Instructions is read.
	Kind          string        `json:"kind"`
	Cadence       schedule.Spec `json:"schedule"`
	EndAt         *time.Time    `json:"end_at,omitempty"`
	NextRunAt     *time.Time    `json:"next_run_at,omitempty"`
	LastRunAt     *time.Time    `json:"last_run_at,omitempty"`
	LastRunStatus string        `json:"last_run_status,omitempty"`

	// LastRunSummary is schedule.Schedule.LastRunSummary, echoed so the UI
	// can say why a run was skipped: "missing connector: gmail" renders as
	// "Skipped · gmail isn't connected".
	LastRunSummary string `json:"last_run_summary,omitempty"`

	// Rev is the backend revision this row is at, echoed so the UI can show
	// staleness and so a client can round-trip it if it ever needs to.
	Rev uint64 `json:"rev,omitempty"`

	// Pending is "" for a confirmed row, or "create" / "update" / "delete"
	// when a locally-made change is still queued.
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
		// the runner already treats as agent — so the UI must be told
		// "agent" rather than left to guess from an empty string.
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
// read-only "Scheduled" section.
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
	items = overlayPendingIntents(items, h.scheduleIntents.List())
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"timezone":  h.scheduleStore.Timezone().String(),
		"schedules": items,
	}))
}

// RunScheduleNow handles POST /api/schedule/:id/run — the web UI's local
// "Run now" button.
func (h *DeviceMQTTHandler) RunScheduleNow(c *gin.Context) {
	id := strings.TrimSpace(c.Param("id"))
	// resolveScheduleRun is the exact lookup schedule_run_handler.go's
	// handleScheduleRun uses for the MQTT path — reused here so "unknown
	// id" can never be defined differently between the two entry points.
	sch, errMsg := resolveScheduleRun(h.scheduleStore, id)
	if errMsg != "" {
		c.JSON(http.StatusNotFound, serializers.ResponseError(errMsg))
		return
	}

	rr, ran := h.scheduleRunner.RunNow(sch)
	if !ran {
		// No ack was published on this path (RunNow never reached
		// SendSystemChatMessage), so this HTTP response is the only place the
		// caller learns "busy, try again".
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

// overlayPendingIntents merges the unconfirmed intent queue onto the
// confirmed rows for display.
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
				ID:           "intent:" + in.IntentID,
				Name:         in.Payload.Name,
				Instructions: in.Payload.Instructions,
				// Resolved, so a pending speak task is not shown as agent.
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
