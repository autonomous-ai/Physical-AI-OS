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
// endpoints.
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

// queueIntent validates, persists and immediately tries to publish one
// intent.
func (h *DeviceMQTTHandler) queueIntent(in schedule.Intent) error {
	if err := h.scheduleIntents.Append(in); err != nil {
		return err
	}
	h.publishIntent(in)
	return nil
}

// CreateSchedule handles POST /api/schedule.
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

	payload := &schedule.IntentPayload{
		Name:         current.Name,
		Instructions: current.Instructions,
		Enabled:      current.Enabled,
		// Seeded from the stored row: an empty kind would be written as "agent".
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

// DeleteSchedule handles DELETE /api/schedule/:id.
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
