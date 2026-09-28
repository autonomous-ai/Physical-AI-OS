package http

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"time"

	migratepersona "go.autonomous.ai/os/system/agent/migrate_persona"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
)

// handleSessionMessageEvent handles "session.message" events for channel (Telegram)
// turns, which OpenClaw 5.x does not emit on the agent lifecycle stream.
func (h *AgentHandler) handleSessionMessageEvent(evt domain.WSEvent) error {
	var sm struct {
		SessionKey string `json:"sessionKey"`
		SessionID  string `json:"sessionId"`
		MessageID  string `json:"messageId"`
		MessageSeq int    `json:"messageSeq"`
		Message    struct {
			Role       string          `json:"role"`
			Content    json.RawMessage `json:"content"`
			StopReason string          `json:"stopReason"`
			Timestamp  int64           `json:"timestamp"`
		} `json:"message"`
		Session struct {
			DisplayName string `json:"displayName"`
			Origin      struct {
				Provider string `json:"provider"`
				Surface  string `json:"surface"`
				Label    string `json:"label"`
				From     string `json:"from"`
			} `json:"origin"`
			DeliveryContext struct {
				Channel string `json:"channel"`
			} `json:"deliveryContext"`
		} `json:"session"`
	}
	if err := json.Unmarshal(evt.Payload, &sm); err != nil {
		slog.Warn("session.message unmarshal error", "component", "agent", "err", err)
		return nil
	}
	// Heartbeat turns must keep the lifecycle path so replies reach the speaker.
	if sm.Session.Origin.Provider == "heartbeat" {
		return nil
	}
	// sessionKey prefix is the stable signal; origin/deliveryContext are best-effort.
	isTelegramChannel := strings.HasPrefix(sm.SessionKey, "agent:main:telegram:") ||
		sm.Session.Origin.Provider == "telegram" ||
		sm.Session.DeliveryContext.Channel == "telegram"
	if !isTelegramChannel {
		return nil
	}
	// Skip sessions the agent lifecycle path already handles (avoids duplicate chat_input).
	const agentLifecycleWindowMs int64 = 30_000
	h.agentLifecycleMu.Lock()
	recentLifecycleMs := h.agentLifecycleAt[sm.SessionKey]
	activeRunID := h.activeRunIDBySession[sm.SessionKey]
	h.agentLifecycleMu.Unlock()
	if recentLifecycleMs > 0 && time.Now().UnixMilli()-recentLifecycleMs < agentLifecycleWindowMs {
		// A Telegram message interleaved into a running device turn: mark that run as
		// a channel run so lifecycle.end suppresses TTS and DMs the reply instead.
		isTelegramChannel := strings.HasPrefix(sm.SessionKey, "agent:main:telegram:") ||
			sm.Session.Origin.Provider == "telegram" ||
			sm.Session.DeliveryContext.Channel == "telegram"
		if sm.Message.Role == "user" && activeRunID != "" && isTelegramChannel {
			// Origin.Provider goes "sticky telegram" on a shared session; skip device echoes.
			msgText := extractMessageContentText(sm.Message.Content)
			if msgText != "" && (isDeviceInternalMessage(msgText) || h.agentGateway.IsRecentOutboundChat(msgText)) {
				// Device echo, not a real interleave.
			} else {
				chatID := extractTelegramChatID(msgText)
				if chatID == "" {
					chatID = extractTelegramIDFromSenderLabel(sm.Session.DisplayName)
				}
				if chatID == "" {
					chatID = extractTelegramIDFromSenderLabel(sm.Session.Origin.Label)
				}
				if chatID != "" {
					h.channelRunsMu.Lock()
					h.channelRuns[activeRunID] = true
					h.interleavedDMByRunID[activeRunID] = chatID
					h.channelRunsMu.Unlock()
					slog.Info("interleaved Telegram message captured — TTS will be suppressed, reply will DM",
						"component", "agent", "sessionKey", sm.SessionKey,
						"active_run_id", activeRunID, "chat_id", chatID)
				} else {
					slog.Warn("interleaved Telegram detected but chat_id not extractable",
						"component", "agent", "sessionKey", sm.SessionKey,
						"display_name", sm.Session.DisplayName,
						"origin_label", sm.Session.Origin.Label)
				}
			}
		}
		slog.Info("session.message skipped — agent lifecycle active",
			"component", "agent", "sessionKey", sm.SessionKey,
			"ageMs", time.Now().UnixMilli()-recentLifecycleMs)
		return nil
	}
	// Skip device chat.send echoes; session.message can arrive before lifecycle.start.
	if sm.Message.Role == "user" {
		text := extractMessageContentText(sm.Message.Content)
		if text != "" && (isDeviceInternalMessage(text) || h.agentGateway.IsRecentOutboundChat(text)) {
			slog.Info("session.message skipped — device-outbound echo",
				"component", "agent", "sessionKey", sm.SessionKey,
				"preview", text[:min(len(text), 80)])
			return nil
		}
	}
	text := extractMessageContentText(sm.Message.Content)

	if sm.Message.Role == "user" {
		runID := "tg-" + sm.MessageID
		if runID == "tg-" {
			runID = fmt.Sprintf("tg-%s-%d", sm.SessionID, sm.MessageSeq)
		}
		senderLabel := sm.Session.DisplayName
		if senderLabel == "" {
			senderLabel = sm.Session.Origin.Label
		}
		telegramID := extractTelegramChatID(text)
		if telegramID == "" {
			telegramID = extractTelegramIDFromSenderLabel(senderLabel)
		}
		h.channelTurnMu.Lock()
		h.channelTurns[sm.SessionKey] = &channelTurnState{
			runID:       runID,
			senderLabel: senderLabel,
			telegramID:  telegramID,
			startedAtMs: sm.Message.Timestamp,
		}
		h.channelTurnMu.Unlock()
		h.channelRunsMu.Lock()
		h.channelRuns[runID] = true
		h.channelRunsMu.Unlock()

		chName := h.agentGateway.GetConfiguredChannel()
		prefix := "[" + chName + "]"
		if senderLabel != "" {
			prefix = "[" + chName + ":" + senderLabel + "]"
		}
		displayMsg := text
		if len(displayMsg) > 200 {
			displayMsg = displayMsg[:200] + "…"
		}
		slog.Info("INBOUND from channel → agent",
			"component", "agent",
			"backend", h.agentGateway.Name(),
			"source", "channel",
			"channel", chName,
			"session_key", sm.SessionKey,
			"run_id", runID,
			"sender", senderLabel,
			"telegram_id", telegramID,
			"msg_len", len(text),
			"message", displayMsg)
		flow.Log("chat_input", map[string]any{
			"run_id":  runID,
			"source":  "channel",
			"message": text,
			"sender":  senderLabel,
		}, runID)
		lcStart := map[string]any{"run_id": runID, "source": "session.message"}
		if st := migratepersona.MemoryState(); st != nil {
			lcStart["memory"] = st
		}
		flow.Log("lifecycle_start", lcStart, runID)
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "chat_input",
			Summary: prefix + " " + displayMsg,
			RunID:   runID,
			Detail:  map[string]string{"role": "user", "message": text, "sender": senderLabel},
		})
		return nil
	}

	if sm.Message.Role != "assistant" {
		return nil
	}
	isFinalAssistant := sm.Message.StopReason == "stop" || sm.Message.StopReason == "end_turn"
	h.channelTurnMu.Lock()
	st, ok := h.channelTurns[sm.SessionKey]
	if !ok {
		h.channelTurnMu.Unlock()
		// Untracked turn: still clear busy so sensing isn't wedged for busyTTL.
		if isFinalAssistant {
			slog.Info("session.message untracked assistant stop — clearing busy",
				"component", "agent", "sessionKey", sm.SessionKey)
			h.agentGateway.SetBusy(false)
		}
		return nil
	}
	if text != "" {
		st.accumulated.WriteString(text)
	}
	isFinal := sm.Message.StopReason == "stop" || sm.Message.StopReason == "end_turn"
	runID := st.runID
	telegramID := st.telegramID
	var fullText string
	if isFinal {
		fullText = st.accumulated.String()
		delete(h.channelTurns, sm.SessionKey)
	}
	h.channelTurnMu.Unlock()
	if !isFinal {
		return nil
	}

	fullText = prunedImageMarkerRe.ReplaceAllString(fullText, "")
	hwCalls, cleanText := extractHWCalls(fullText)
	cleanText = extractSayTag(cleanText)
	cleanText = sanitizeAgentText(cleanText)
	chFilter := newCoTLeakFilter(h.replyLanguageCode())
	if filtered := chFilter.filterText(cleanText); len(chFilter.dropped) > 0 {
		slog.Warn("CoT leak dropped from channel turn reply",
			"component", "agent", "run_id", runID,
			"dropped", len(chFilter.dropped),
			"preview", cotDroppedPreview(chFilter.dropped, 200))
		cleanText = filtered
	}

	// Drain the per-run count so the map doesn't leak.
	_ = h.consumeFiredHWCount(runID)

	h.fireHWCalls(hwCalls, runID)

	flow.Log("lifecycle_end", map[string]any{
		"run_id": runID,
		"source": "session.message",
	}, runID)
	// Agent-path lifecycle.end never fires for channel turns; clear busy here.
	h.agentGateway.SetBusy(false)

	// Channel turns never speak on the device; OpenClaw delivers the reply.
	switch {
	case isAgentNoReply(cleanText):
		slog.Info("channel turn replied NO_REPLY", "component", "agent", "run_id", runID)
		flow.Log("no_reply", map[string]any{"run_id": runID}, runID)
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "chat_response",
			Summary: "[no reply]",
			RunID:   runID,
			State:   "final",
			Detail:  map[string]string{"role": "assistant", "message": "[no reply]"},
		})
	case strings.TrimSpace(cleanText) == "":
		slog.Info("channel turn HW-only reply", "component", "agent", "run_id", runID, "hw_calls", len(hwCalls))
		flow.Log("hw_only_reply", map[string]any{"run_id": runID}, runID)
	default:
		preview := cleanText
		if len(preview) > 200 {
			preview = preview[:200] + "…"
		}
		slog.Info("channel turn final assistant text", "component", "agent",
			"run_id", runID, "hw_calls", len(hwCalls), "telegram_id", telegramID, "text", preview)
		flow.Log("tts_suppressed", map[string]any{
			"run_id": runID,
			"reason": "channel_run",
			"text":   cleanText,
		}, runID)
		h.monitorBus.Push(domain.MonitorEvent{
			Type:    "chat_response",
			Summary: preview,
			RunID:   runID,
			State:   "final",
			Detail:  map[string]string{"role": "assistant", "message": cleanText},
		})
		// No device-side DM: OpenClaw fans out the reply (a DM here duplicated it).
	}
	h.channelRunsMu.Lock()
	delete(h.channelRuns, runID)
	h.channelRunsMu.Unlock()

	return nil
}
