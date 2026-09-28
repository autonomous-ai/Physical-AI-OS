package http

import (
	"encoding/json"
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/telemetry"
)

// handleChatEvent handles WS "chat" events (errors, empty/slash finals, assistant/partial pushes).
func (h *AgentHandler) handleChatEvent(evt domain.WSEvent) error {
	slog.Debug("chat raw payload", "component", "agent", "payload", string(evt.Payload))
	var payload domain.ChatPayload
	if err := json.Unmarshal(evt.Payload, &payload); err != nil {
		slog.Error("chat parse error", "component", "agent", "error", err, "raw", string(evt.Payload))
		return nil
	}
	payload.ResolveChatMessage()
	slog.Info(">>> CHAT EVENT RECEIVED", "component", "agent",
		"run_id", payload.RunID,
		"role", payload.Role,
		"state", payload.State,
		"message_len", len(payload.Message),
		"message", payload.Message,
		"raw_message", string(payload.RawMessage))
	// OpenClaw may send a UUID while lifecycle/tool/tts used the resolved device id.
	flowRunID := h.resolveRunID(payload.RunID)
	// Harness owns this run's final response; don't close the Web/MQTT stream with the handoff.
	h.harnessRepliesMu.Lock()
	_, harnessOwnsReply := h.harnessReplies[flowRunID]
	h.harnessRepliesMu.Unlock()
	if harnessOwnsReply && payload.Role != "user" && payload.State != "error" {
		return nil
	}

	if strings.HasPrefix(flowRunID, "device-") {
		msgPreview := payload.Message
		msgPreview = strings.ReplaceAll(msgPreview, "\n", " ")
		if len(msgPreview) > 120 {
			msgPreview = msgPreview[:120] + "…"
		}
		slog.Info("openclaw chat event (device)", "component", "agent",
			"backend", h.agentGateway.Name(),
			"openclaw_run_id", payload.RunID,
			"flow_run_id", flowRunID,
			"role", payload.Role,
			"state", payload.State,
			"has_message", strings.TrimSpace(msgPreview) != "",
			"message_preview", msgPreview)
	}
	if payload.RunID != "" && flowRunID != payload.RunID {
		slog.Info("flow correlation", "op", "chat_run_resolve", "section", "openclaw_chat",
			"openclaw_run_id", payload.RunID, "device_run_id", flowRunID,
			"role", payload.Role, "state", payload.State)
	}

	// OpenClaw never broadcasts role:"user" on the chat stream; user input comes via lifecycle_start.

	// Skip the error banner when the reply was already recovered (handler_error_recovery.go),
	// including the gateway's ~15s-later retry error.
	if payload.State == "error" {
		defer hal.EndVoiceFollowup(flowRunID)
		errMsg := payload.ErrorMessage
		if errMsg == "" {
			errMsg = "unknown error"
		}
		if h.wasErrorRecovered(flowRunID) {
			slog.Info("OpenClaw chat error suppressed — reply already recovered",
				"component", "agent", "run_id", flowRunID, "error", errMsg)
		} else {
			slog.Error("OpenClaw chat error", "component", "agent", "run_id", flowRunID, "error", errMsg)
			flow.Log("agent_error", map[string]any{"run_id": flowRunID, "error": errMsg}, flowRunID)
			telemetry.ReportTaskExecution(h.resolveTaskRunID(payload.RunID, flowRunID), "", "failed", "chat_error")
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "chat_response",
				Summary: "❌ " + shortError(errMsg),
				RunID:   flowRunID,
				State:   "error",
				Error:   shortError(errMsg),
				Detail:  map[string]string{"error": shortError(errMsg)},
			})
		}
	}

	// Record (not interpret) an empty final for a device runId that never opened a lifecycle
	// (pendingChatTrace entry still present).
	isDeviceOutboundFinal := payload.State == "final" && isDeviceOutboundChatRunID(flowRunID)
	isEmptyFinalNoLifecycle := isDeviceOutboundFinal &&
		strings.TrimSpace(payload.Message) == "" &&
		h.agentGateway.RemovePendingChatTraceByRunID(flowRunID)
	if isEmptyFinalNoLifecycle {
		defer hal.EndVoiceFollowup(flowRunID)
		slog.Info("chat final empty, no lifecycle for runId",
			"component", "agent", "run_id", flowRunID)
		flow.Log("chat_final_empty", map[string]any{
			"run_id":            flowRunID,
			"state":             "final",
			"message_empty":     true,
			"lifecycle_started": false,
		}, flowRunID)
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "chat_response",
			Summary: "(empty final, no lifecycle)",
			RunID:   flowRunID,
			State:   "final",
			Detail: map[string]string{
				"message_empty":     "true",
				"lifecycle_started": "false",
			},
		})
		// No lifecycle.end will fire for this run: release busy now or events queue for busyTTL (5 min).
		h.agentGateway.SetBusy(false)
	}

	// Slash commands emit a final without a lifecycle; close the flow turn here. A true Remove proves
	// no lifecycle ran, and the empty-final branch above already consumed it, so no double-emit.
	isSlashFinalOk := isDeviceOutboundFinal &&
		!isEmptyFinalNoLifecycle &&
		strings.TrimSpace(payload.Message) != "" &&
		h.agentGateway.RemovePendingChatTraceByRunID(flowRunID)
	if isSlashFinalOk {
		defer hal.EndVoiceFollowup(flowRunID)
		slog.Info("chat final ok, no lifecycle for runId (slash dispatcher)",
			"component", "agent", "run_id", flowRunID)
		// Include the (truncated) reply so Flow Monitor can render OUT for no-lifecycle turns.
		msgPreview := payload.Message
		if len(msgPreview) > 500 {
			msgPreview = msgPreview[:500] + "…"
		}
		flow.Log("chat_final_ok", map[string]any{
			"run_id":            flowRunID,
			"state":             "final",
			"message_empty":     false,
			"lifecycle_started": false,
			"message":           msgPreview,
		}, flowRunID)
		telemetry.ReportTaskExecution(flowRunID, "", "completed", "chat_final_no_lifecycle")
		// No lifecycle.end for slash commands: release busy or it wedges for busyTTL (5 min).
		h.agentGateway.SetBusy(false)
	}

	// Skip the generic empty-final emit when chat_final_empty was already pushed above.
	if payload.Role != "user" && payload.State != "error" && !isEmptyFinalNoLifecycle {
		summary := payload.Message
		if len(summary) > 120 {
			summary = summary[:120] + "..."
		}
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "chat_response",
			Summary: summary,
			RunID:   flowRunID,
			State:   payload.State,
			Detail: map[string]string{
				"role":    payload.Role,
				"message": payload.Message,
			},
		})
	}

	// TTS comes from the lifecycle_end path; the chat final is not spoken to avoid double speech.

	return nil
}
