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

// handleChatEvent handles WS event=="chat": OpenClaw chat-stream events
// (errors, empty/slash finals, assistant/partial pushes). Extracted verbatim
// from HandleEvent.
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
	// Same as agent stream: OpenClaw may send UUID while lifecycle/tool/tts used resolved device id.
	flowRunID := h.resolveRunID(payload.RunID)
	// Harness owns the final response for this exact run. Do not close the
	// Web/MQTT stream with the device agent's handoff before it arrives.
	h.harnessRepliesMu.Lock()
	_, harnessOwnsReply := h.harnessReplies[flowRunID]
	h.harnessRepliesMu.Unlock()
	if harnessOwnsReply && payload.Role != "user" && payload.State != "error" {
		return nil
	}

	// Debug alignment: OpenClaw "chat" stream may or may not include user messages for outbound chat.send.
	// When flowRunID belongs to the device, log role/state/message so we can confirm whether chat_input can be emitted.
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

	// (OpenClaw gateway never broadcasts role:"user" on the chat stream.
	// User messages are captured via lifecycle_start + chat.history above.)

	// Chat error: OpenClaw reports agent processing failure. Skip the banner
	// when the reply was already salvaged from this run's deltas/history
	// (see handler_error_recovery.go) — covers both the initial incomplete-
	// turn error and the gateway's ~15s-later retry error.
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

	// Factual detection: OpenClaw sent a `state:"final"` chat event with
	// empty Message for a device-format runId, and the device never opened a
	// lifecycle for that runId (pendingChatTrace entry still present —
	// lifecycle_start would have removed it; see ~line 84).
	//
	// We record only what we observe; we do NOT infer "steered" /
	// "merged" / "self-reply" — those are downstream interpretations
	// the operator makes from the timeline (e.g. a UUID lifecycle
	// arriving later with matching input).
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
		// No lifecycle.end will fire for this run — release the busy flag
		// here so subsequent sensing/chat events aren't queued for the
		// full busyTTL (5 min). chat.send sets activeTurn=true before
		// every write, including slash commands and steered/merged turns
		// that resolve via this empty-final path.
		h.agentGateway.SetBusy(false)
	}

	// Slash commands (e.g. /status, /new) are pre-LLM dispatched by OpenClaw
	// so they emit `state:"final"` with the reply payload but never open a
	// lifecycle. Without a closing flow event, Flow Monitor renders the turn
	// as active forever. Mirror chat_final_empty but record the success case.
	// RemovePendingChatTraceByRunID is the no-lifecycle witness: lifecycle_start
	// removes the entry, so a returning true here proves no lifecycle ran.
	// The existing isEmptyFinalNoLifecycle check above already consumed the
	// pending entry when it fires, so this Remove call is naturally false
	// when both conditions could match — no double-emit possible.
	isSlashFinalOk := isDeviceOutboundFinal &&
		!isEmptyFinalNoLifecycle &&
		strings.TrimSpace(payload.Message) != "" &&
		h.agentGateway.RemovePendingChatTraceByRunID(flowRunID)
	if isSlashFinalOk {
		defer hal.EndVoiceFollowup(flowRunID)
		slog.Info("chat final ok, no lifecycle for runId (slash dispatcher)",
			"component", "agent", "run_id", flowRunID)
		// Include the reply payload (truncated like chat_input) so Flow
		// Monitor can render OUT for slash turns — without it, turnIO
		// has no source for the output field on these no-lifecycle turns.
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
		// This successful final is the execution boundary for a command
		// that bypassed lifecycle events. Empty finals remain unproven.
		telemetry.ReportTaskExecution(flowRunID, "", "completed", "chat_final_no_lifecycle")
		// Slash commands bypass the LLM lifecycle so lifecycle.end never
		// fires for this run. Without this, every /status (or /new etc.)
		// wedges the busy flag for the full busyTTL (5 min), queueing
		// every subsequent sensing/chat event behind it.
		h.agentGateway.SetBusy(false)
	}

	// Push assistant/partial chat events to monitor (user input tracked via lifecycle_start — already tracked as chat_input).
	// Skip the generic empty-final emit when we already pushed the factual chat_final_empty event above.
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

	// TTS is sent from the lifecycle_end path above (assistant delta accumulation).
	// The chat stream's final message is not used for TTS to avoid speaking responses twice.

	return nil
}
