package http

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
)

// handleSessionToolEvent handles WS "session.tool" events for session-subscribed clients.
func (h *AgentHandler) handleSessionToolEvent(evt domain.WSEvent) error {
	var payload domain.AgentPayload
	if err := json.Unmarshal(evt.Payload, &payload); err != nil {
		slog.Warn("session.tool unmarshal error", "component", "agent", "err", err)
		return nil
	}
	// Map the OpenClaw UUID to the synthetic device runId so tool/hw flow events share chat_input's run_id.
	if payload.SessionKey != "" && payload.RunID != "" {
		h.channelTurnMu.Lock()
		if st, ok := h.channelTurns[payload.SessionKey]; ok && st.runID != "" {
			h.mapRunID(payload.RunID, st.runID)
		}
		h.channelTurnMu.Unlock()
	}
	flowRunID := h.resolveRunID(payload.RunID)
	toolName := payload.ToolName()
	toolArgs := payload.ToolArguments()
	summary := toolName
	if payload.Data.Phase == "start" {
		summary = fmt.Sprintf("Tool %s started", toolName)
		h.rememberToolArgs(payload.Data.ToolCallID, toolArgs)
		// Fire [HW:...] markers echoed via a shell tool call (they never reach the reply-text
		// interceptor); skip the cosmetic detection below to avoid duplicate flow nodes.
		echoedHW := h.fireEchoedHWMarkers(toolName, toolArgs, flowRunID)
		// Do NOT suppress TTS on /audio/play: the reply must play before music (music_service waits for TTS).
		if !echoedHW && strings.Contains(toolArgs, "/audio/play") {
			h.monitorBus.Push(domain.MonitorEvent{Type: "hw_audio", Summary: toolArgs, RunID: flowRunID})
			flow.Log("hw_audio", map[string]any{"args": toolArgs, "run_id": flowRunID}, flowRunID)
		}
		if !echoedHW && strings.Contains(toolArgs, "/emotion") {
			h.monitorBus.Push(domain.MonitorEvent{Type: "led_set", Summary: "agent tool: " + toolName})
			h.monitorBus.Push(domain.MonitorEvent{Type: "hw_emotion", Summary: toolArgs, RunID: flowRunID})
			flow.Log("hw_emotion", map[string]any{"args": toolArgs, "run_id": flowRunID}, flowRunID)
			if e := parseEmotion(toolArgs); e != "" {
				h.lastEmotionMu.Lock()
				h.lastEmotion = e
				h.lastEmotionMu.Unlock()
			}
		} else if strings.Contains(toolArgs, "/led/solid") ||
			strings.Contains(toolArgs, "/led/effect") ||
			strings.Contains(toolArgs, "/scene") {
			h.monitorBus.Push(domain.MonitorEvent{Type: "led_set", Summary: "agent tool: " + toolName})
			h.monitorBus.Push(domain.MonitorEvent{Type: "hw_led", Summary: toolArgs, RunID: flowRunID})
			flow.Log("hw_led", map[string]any{"args": toolArgs, "run_id": flowRunID}, flowRunID)
		}
		if !echoedHW && strings.Contains(toolArgs, "/led/off") {
			h.monitorBus.Push(domain.MonitorEvent{Type: "led_off", Summary: "agent tool: " + toolName})
			h.monitorBus.Push(domain.MonitorEvent{Type: "hw_led", Summary: toolArgs, RunID: flowRunID})
			flow.Log("hw_led", map[string]any{"args": toolArgs, "run_id": flowRunID}, flowRunID)
		}
		if !echoedHW && (strings.Contains(toolArgs, "/servo/aim") || strings.Contains(toolArgs, "/servo/play")) {
			h.monitorBus.Push(domain.MonitorEvent{Type: "hw_servo", Summary: toolArgs, RunID: flowRunID})
			flow.Log("hw_servo", map[string]any{"args": toolArgs, "run_id": flowRunID}, flowRunID)
		}
		// Intercept OpenClaw built-in tts tool (session.tool path).
		if toolName == "tts" {
			if ttsText := extractTTSText(toolArgs); ttsText != "" {
				isChannelRun := isChannelOriginatedRun(payload.RunID, flowRunID)
				isWebChat := h.agentGateway.IsWebChatRun(flowRunID)
				isSilent := h.agentGateway.IsSilentRun(flowRunID)
				slog.Info("intercepted built-in tts tool (session.tool), routing to HAL", "component", "agent", "run_id", flowRunID, "text", ttsText[:min(len(ttsText), 80)], "channel_run", isChannelRun, "web_chat", isWebChat, "silent", isSilent)
				flow.Log("tts_send", map[string]any{"run_id": flowRunID, "text": ttsText, "source": "tts_tool_intercept"}, flowRunID)
				if !isChannelRun && !isWebChat && !isSilent {
					h.deliverToolTTS(ttsText, flowRunID, "TTS intercept delivery failed")
				}
				h.suppressTTS(payload.RunID, "already_spoken")
			}
		}
	} else if payload.Data.Phase == "end" {
		result := payload.ResultText()
		if len(result) > 100 {
			result = result[:100] + "..."
		}
		summary = fmt.Sprintf("Tool %s done", toolName)
		if result != "" {
			summary += ": " + result
		}
	}
	toolFlowData := map[string]any{"tool": toolName, "phase": payload.Data.Phase, "run_id": flowRunID, "source": "session.tool", "args": toolArgs}
	if snapshotURL := h.snapshotURLForToolCall(payload.Data.ToolCallID, toolArgs, payload.ResultText()); snapshotURL != "" {
		toolFlowData["snapshot_url"] = snapshotURL
	}
	flow.Log("tool_call", toolFlowData, flowRunID)
	toolDetail := map[string]string{"tool": toolName, "args": toolArgs}
	if snapshotURL, ok := toolFlowData["snapshot_url"].(string); ok {
		toolDetail["snapshot_url"] = snapshotURL
	}
	h.monitorBus.Push(domain.MonitorEvent{
		Type:    "tool_call",
		Summary: summary,
		RunID:   flowRunID,
		Phase:   payload.Data.Phase,
		Detail:  toolDetail,
	})

	return nil
}
