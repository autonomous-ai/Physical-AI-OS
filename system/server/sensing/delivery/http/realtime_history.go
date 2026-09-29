package http

import (
	"log/slog"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/telemetry"
)

// SetRealtimeHistory replaces the volatile sensing queue for completed realtime
// exchanges. Speaker supersession still runs before this persistence hook.
func (h *SensingHandler) SetRealtimeHistory(fn func(string, string) (string, error)) {
	h.realtimeHistory = fn
}

func (h *SensingHandler) persistRealtimeHistory(c *gin.Context, req SensingEventRequest, suppressed bool) {
	historyRunID, err := h.realtimeHistory(req.InteractionID, reSnapshotPath.ReplaceAllString(req.Message, ""))
	if err != nil {
		slog.Error("persist realtime history", "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("Could not save realtime conversation history"))
		return
	}
	runID := "device-realtime-" + strings.TrimPrefix(historyRunID, "device-chat-context-")
	input, reply, _ := strings.Cut(strings.TrimPrefix(req.Message, "[skills: input-branching]\n[HANDLED] "), "\n[REPLY] ")
	input = strings.TrimPrefix(input, "[HANDLED] ")
	telemetry.ReportTaskStarted(req.Type, req.InteractionID, runID)
	start := flow.Start("sensing_input", map[string]any{
		"type": req.Type, "message": req.Message, "interaction_id": req.InteractionID,
		"route": "realtime", "history_run_id": historyRunID, "voice_turn_type": req.voiceTurnType(),
	}, runID)
	flow.End("sensing_input", start, map[string]any{"path": "realtime"}, runID)
	flow.Log("realtime_response", map[string]any{
		"input": input, "text": strings.TrimSpace(reSnapshotPath.ReplaceAllString(reply, "")),
		"history_run_id": historyRunID, "voice_turn_type": req.voiceTurnType(),
	}, runID)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(map[string]any{
		"handler": "external_history", "runId": runID, "historyRunId": historyRunID, "speechSuppressed": suppressed,
	}))
}

// A display hint cannot grant wake authorization or change event dispatch.
func (req SensingEventRequest) voiceTurnType() string {
	switch req.Type {
	case "voice", "voice_command", "voice_followup", "voice_agent_handled":
	default:
		return ""
	}
	switch req.VoiceTurnType {
	case "voice", "voice_command", "voice_followup":
		return req.VoiceTurnType
	}
	switch req.Type {
	case "voice", "voice_command", "voice_followup":
		return req.Type
	}
	return ""
}
