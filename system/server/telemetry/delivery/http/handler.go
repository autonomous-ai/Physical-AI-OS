// Package http exposes the device-local ingestion endpoint for telemetry
// events produced outside os-server — HAL today, any other on-device
// process later.
package http

import (
	"log/slog"
	"net/http"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/telemetry"
)

// maxParams bounds one event. Generous for a metrics record, small enough that a
// looping producer cannot post a payload worth forwarding to AA.
const maxParams = 64

// EventRequest is the wire shape of POST /api/telemetry/event.
type EventRequest struct {
	EventName string         `json:"event_name" binding:"required"`
	EventID   string         `json:"event_id"`
	Params    map[string]any `json:"params"`
}

// TelemetryHandler serves the ingestion endpoint. Stateless — the queue and
// the de-duplication live in system/telemetry.
type TelemetryHandler struct{}

func ProvideTelemetryHandler() *TelemetryHandler { return &TelemetryHandler{} }

// PostEvent accepts one event and queues it.
func (h *TelemetryHandler) PostEvent(c *gin.Context) {
	var req EventRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("invalid telemetry event: "+err.Error()))
		return
	}
	if len(req.Params) > maxParams {
		slog.Warn("[telemetry] event rejected -- too many params", "component", "telemetry",
			"event_name", req.EventName, "params", len(req.Params))
		c.JSON(http.StatusBadRequest, serializers.ResponseError("too many params"))
		return
	}

	telemetry.Report(telemetry.Event{
		Name:   req.EventName,
		ID:     req.EventID,
		Params: req.Params,
	})
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"accepted": true}))
}
