package mqtthandler

import (
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/schedule"
	"go.autonomous.ai/os/system/server/serializers"
)

// scheduleWriteRequest is the JSON body of the device-side create/update
// endpoints. Only the user-editable subset — id, rev, status and every run
// bookkeeping field are backend-owned and have no representation here, so the
// local UI cannot propose them any more than the wire payload can.
//
// Enabled is a POINTER so "not supplied" is distinguishable from "false" on a
// PATCH: without that, editing only a task's name would silently pause it.
type scheduleWriteRequest struct {
	Name         string `json:"name"`
	Instructions string `json:"instructions"`
	Enabled      *bool  `json:"enabled,omitempty"`
	// Kind is "agent" (default when omitted) or "speak" — see
	// schedule.Schedule.Kind. Omitting it keeps today's behaviour exactly.
	Kind         string        `json:"kind,omitempty"`
	TemplateCode string        `json:"template_code,omitempty"`
	Cadence      schedule.Spec `json:"schedule"`
	EndAt        *time.Time    `json:"end_at,omitempty"`
}

// queueIntent validates, persists and immediately tries to publish one intent.
//
// The publish is best-effort ON PURPOSE and its failure is not reported to the
// caller: the intent is already durably queued, so an offline device has
// genuinely accepted the user's edit and will deliver it on reconnect.
// Returning an error here would tell the user their change was lost when it
// was not.
func (h *DeviceMQTTHandler) queueIntent(in schedule.Intent) error {
	if err := h.scheduleIntents.Append(in); err != nil {
		return err
	}
	h.publishIntent(in)
	return nil
}

// CreateSchedule handles POST /api/schedule.
//
// Note what this does NOT do: it does not write to schedules.json. A task the
// backend has not confirmed never enters the file the runner reads, so it
// cannot fire early. The UI still shows it immediately, because ListSchedules
// overlays the pending queue — visible, but not yet armed.
//
// Raises one ops alert per request (schedule_alerts.go): "✅ Schedule
// created: <name>" once the proposal is durably queued — which is the moment
// the device has accepted the user's task, even offline — or "❌ Schedule
// create: <name> — FAILED" with the reason. The backend's confirmation shows
// up separately, as the next schedule.sync alert's "created: <name>".
func (h *DeviceMQTTHandler) CreateSchedule(c *gin.Context) {
	name := "" // unknown until the body parses
	fail := func(status int, msg string) {
		h.alertScheduleEvent(scheduleOpFailedTitle("create", name), msg)
		c.JSON(status, serializers.ResponseError(msg))
	}

	var req scheduleWriteRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		fail(http.StatusBadRequest, "invalid body: "+err.Error())
		return
	}
	name = strings.TrimSpace(req.Name)

	enabled := true
	if req.Enabled != nil {
		enabled = *req.Enabled
	}
	payload := &schedule.IntentPayload{
		Name:         strings.TrimSpace(req.Name),
		Instructions: strings.TrimSpace(req.Instructions),
		Enabled:      enabled,
		Kind:         schedule.ResolveKind(req.Kind),
		TemplateCode: req.TemplateCode,
		Cadence:      req.Cadence,
		EndAt:        req.EndAt,
		Timezone:     h.scheduleStore.Timezone().String(),
	}
	if err := schedule.ValidateIntentPayload(payload); err != nil {
		fail(http.StatusBadRequest, err.Error())
		return
	}

	intentID, err := schedule.NewIntentID()
	if err != nil {
		fail(http.StatusInternalServerError, err.Error())
		return
	}
	in := schedule.Intent{
		IntentID:  intentID,
		Op:        "create",
		Payload:   payload,
		CreatedAt: time.Now(),
	}
	if err := h.queueIntent(in); err != nil {
		fail(http.StatusInternalServerError, err.Error())
		return
	}

	h.alertScheduleEvent("✅ Schedule created: "+name,
		"queued for backend confirmation (intent "+intentID+")")
	c.JSON(http.StatusAccepted, serializers.ResponseSuccess(map[string]any{
		"intent_id": intentID,
		"pending":   "create",
		"schedule":  payload,
	}))
}

// UpdateSchedule handles PATCH /api/schedule/:id.
//
// The stored row's rev is quoted as base_rev — this is the compare half of the
// backend's compare-and-swap, and the reason an edit made on a device that has
// been offline cannot silently overwrite a newer edit made in the app.
func (h *DeviceMQTTHandler) UpdateSchedule(c *gin.Context) {
	id := strings.TrimSpace(c.Param("id"))
	current, ok := h.scheduleStore.Get(id)
	if !ok {
		c.JSON(http.StatusNotFound, serializers.ResponseError("unknown schedule id: "+id))
		return
	}

	var req scheduleWriteRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid body: "+err.Error()))
		return
	}

	// Start from what is stored and layer the supplied fields on top, so a
	// PATCH that names only one field leaves the rest of the task alone.
	payload := &schedule.IntentPayload{
		Name:         current.Name,
		Instructions: current.Instructions,
		Enabled:      current.Enabled,
		// Seeded from the stored row, not left blank: the proposal carries the
		// whole mutable subset, so an unmentioned kind must be re-stated rather
		// than sent as "" — which the backend would write as "agent".
		Kind:     schedule.ResolveKind(current.Kind),
		Cadence:  current.Cadence,
		EndAt:    current.EndAt,
		Timezone: h.scheduleStore.Timezone().String(),
	}
	if s := strings.TrimSpace(req.Name); s != "" {
		payload.Name = s
	}
	if s := strings.TrimSpace(req.Instructions); s != "" {
		payload.Instructions = s
	}
	if req.Enabled != nil {
		payload.Enabled = *req.Enabled
	}
	if strings.TrimSpace(req.Kind) != "" {
		payload.Kind = schedule.ResolveKind(req.Kind)
	}
	if req.Cadence.Repeat != "" {
		payload.Cadence = req.Cadence
	}
	if req.EndAt != nil {
		payload.EndAt = req.EndAt
	}
	if err := schedule.ValidateIntentPayload(payload); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}

	intentID, err := schedule.NewIntentID()
	if err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	in := schedule.Intent{
		IntentID:   intentID,
		Op:         "update",
		ScheduleID: id,
		BaseRev:    current.Rev,
		Payload:    payload,
		CreatedAt:  time.Now(),
	}
	if err := h.queueIntent(in); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}

	c.JSON(http.StatusAccepted, serializers.ResponseSuccess(map[string]any{
		"intent_id": intentID,
		"pending":   "update",
		"id":        id,
		"base_rev":  current.Rev,
		"schedule":  payload,
	}))
}

// DeleteSchedule handles DELETE /api/schedule/:id. Like the other two writes
// this only queues a proposal — the row stays in schedules.json, and therefore
// stays runnable, until the backend confirms the delete and the resulting
// schedule.sync removes it. That is deliberate: a delete that the backend
// rejects must not have already stopped the task.
//
// Alerts like CreateSchedule: "✅ Schedule deleted: <name>" once the proposal
// is queued, "❌ Schedule delete: <name or id> — FAILED" otherwise.
func (h *DeviceMQTTHandler) DeleteSchedule(c *gin.Context) {
	id := strings.TrimSpace(c.Param("id"))
	subject := id // the name, once the row is found
	fail := func(status int, msg string) {
		h.alertScheduleEvent(scheduleOpFailedTitle("delete", subject), msg)
		c.JSON(status, serializers.ResponseError(msg))
	}

	current, ok := h.scheduleStore.Get(id)
	if !ok {
		fail(http.StatusNotFound, "unknown schedule id: "+id)
		return
	}
	subject = scheduleDisplayName(current.ID, current.Name)

	intentID, err := schedule.NewIntentID()
	if err != nil {
		fail(http.StatusInternalServerError, err.Error())
		return
	}
	in := schedule.Intent{
		IntentID:   intentID,
		Op:         "delete",
		ScheduleID: id,
		BaseRev:    current.Rev,
		CreatedAt:  time.Now(),
	}
	if err := h.queueIntent(in); err != nil {
		fail(http.StatusInternalServerError, err.Error())
		return
	}

	h.alertScheduleEvent("✅ Schedule deleted: "+subject,
		"queued for backend confirmation (intent "+intentID+")")
	c.JSON(http.StatusAccepted, serializers.ResponseSuccess(map[string]any{
		"intent_id": intentID,
		"pending":   "delete",
		"id":        id,
		"base_rev":  current.Rev,
	}))
}
