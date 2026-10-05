package http

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"regexp"
	"strings"
	"time"

	migratepersona "go.autonomous.ai/os/system/agent/migrate_persona"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
	"go.autonomous.ai/os/system/telemetry"
)

// hwHostRE matches a HAL/os-server loopback endpoint in a tool call; anchoring on
// the host separates a call from a mere mention (e.g. `cat …/emotion/SKILL.md`, #342).
var hwHostRE = regexp.MustCompile(`127\.0\.0\.1:500[01](/[A-Za-z0-9_./-]*)`)

// hwPathFromToolArgs returns the first endpoint path a tool call targets, or "" for a
// mere mention. Query strings and trailing punctuation are trimmed.
func hwPathFromToolArgs(toolArgs string) string {
	m := hwHostRE.FindStringSubmatch(toolArgs)
	if len(m) != 2 {
		return ""
	}
	path := m[1]
	if i := strings.IndexAny(path, "?'\"` "); i >= 0 {
		path = path[:i]
	}
	return strings.TrimRight(path, "/.")
}

// servoMovementPaths are servo endpoints that move the body; reads are deliberately absent.
var servoMovementPaths = map[string]bool{
	"/servo/aim":    true,
	"/servo/play":   true,
	"/servo/nudge":  true,
	"/servo/search": true,
	"/servo/demo":   true,
}

func isServoMovementPath(path string) bool { return servoMovementPaths[path] }

// handleAgentStreamEvent handles WS event=="agent" (lifecycle/tool/thinking/assistant)
// plus the lifecycle-end TTS flush.
func (h *AgentHandler) handleAgentStreamEvent(evt domain.WSEvent) error {
	var payload domain.AgentPayload
	if err := json.Unmarshal(evt.Payload, &payload); err != nil {
		return err
	}
	if payload.SessionKey != "" && h.agentGateway.GetSessionKey() == "" {
		h.agentGateway.SetSessionKey(payload.SessionKey)
	}

	// Map OpenClaw run ID → device trace on lifecycle start (device session only; group
	// sessions must not merge into sensing traces). UUID runs are matched by content via
	// chat.history, synchronously, so every later event resolves to the device ID.
	agentSession := h.agentGateway.GetSessionKey()
	isAgentSession := agentSession != "" && payload.SessionKey == agentSession
	if payload.Stream == "lifecycle" && payload.Data.Phase == "start" && payload.RunID != "" && isAgentSession {
		if isDeviceOutboundChatRunID(payload.RunID) {
			h.agentGateway.RemovePendingChatTraceByRunID(payload.RunID)
		} else {
			hist, err := h.agentGateway.FetchChatHistory(payload.SessionKey, 5)
			if err == nil && hist != nil {
				if userMsg, _, _ := extractLastUserMessageFromHistory(hist); userMsg != "" {
					h.correlateTaskRun(payload.RunID, userMsg)
					if deviceTrace := h.agentGateway.MatchPendingByMessage(userMsg); deviceTrace != "" {
						h.mapRunID(payload.RunID, deviceTrace)
						slog.Info("mapped OpenClaw runId to device trace via chat.history",
							"component", "agent", "openclawId", payload.RunID, "deviceId", deviceTrace)
						slog.Info("flow correlation", "op", "openclaw_uuid_map", "section", "openclaw",
							"openclaw_run_id", payload.RunID, "device_run_id", deviceTrace,
							"note", "matched via chat.history last user message text")
					}
				}
			} else if err != nil {
				slog.Warn("chat.history fetch failed at UUID lifecycle_start (skipping map)",
					"component", "agent", "run_id", payload.RunID, "err", err)
			}
		}
	}

	flowRunID := h.resolveRunID(payload.RunID)
	if payload.Stream == "lifecycle" && (payload.Data.Phase == "end" || payload.Data.Phase == "error") {
		// Register final reply submissions before releasing processing (HAL waits on admission).
		defer hal.EndVoiceFollowup(flowRunID)
	}
	switch payload.Stream {
	case "lifecycle":
		slog.Info("lifecycle event", "component", "agent", "phase", payload.Data.Phase, "runId", payload.RunID, "flowRunId", flowRunID, "session", payload.SessionKey)
		if payload.Data.Phase == "start" {
			h.ttsTurnSequence(flowRunID)
		}

		// Track agent-path activity per session so session.message skips turns the agent stream
		// already drives; cleared on end/error so the next channel turn isn't skipped.
		if payload.SessionKey != "" && !payload.Data.MergedIntoActiveTurn {
			h.agentLifecycleMu.Lock()
			switch payload.Data.Phase {
			case "start":
				h.agentLifecycleAt[payload.SessionKey] = time.Now().UnixMilli()
				if payload.RunID != "" {
					h.activeRunIDBySession[payload.SessionKey] = payload.RunID
				}
			case "end", "error":
				// A delayed terminal event must not clear a newer host turn.
				if h.activeRunIDBySession[payload.SessionKey] == payload.RunID {
					delete(h.agentLifecycleAt, payload.SessionKey)
					delete(h.activeRunIDBySession, payload.SessionKey)
				}
			}
			h.agentLifecycleMu.Unlock()
		}

		// Correlate with queued cron "started" events (no runId/sessionKey): consume the oldest
		// within cronFireWindowMs. UUID runIds only, so device turns can't claim a cron slot.
		if payload.Data.Phase == "start" && payload.RunID != "" && !isDeviceOutboundChatRunID(payload.RunID) {
			now := time.Now().UnixMilli()
			cutoff := now - cronFireWindowMs
			h.cronFireExpectedMu.Lock()
			idx := 0
			for idx < len(h.cronFireExpected) && h.cronFireExpected[idx] < cutoff {
				idx++
			}
			h.cronFireExpected = h.cronFireExpected[idx:]
			if len(h.cronFireExpected) > 0 {
				startedAt := h.cronFireExpected[0]
				h.cronFireExpected = h.cronFireExpected[1:]
				h.cronFireExpectedMu.Unlock()
				h.cronFireRunsMu.Lock()
				h.cronFireRuns[payload.RunID] = true
				h.cronFireRunsMu.Unlock()
				slog.Info("cron fire correlated — will force TTS", "component", "agent", "run_id", payload.RunID, "session", payload.SessionKey, "delta_ms", now-startedAt)
				flow.Log("cron_fire", map[string]any{"run_id": payload.RunID, "delta_ms": now - startedAt}, payload.RunID)
			} else {
				h.cronFireExpectedMu.Unlock()
			}
		}

		// External channel turn: UUID run_id (not device-chat-*), excluding cron-fire turns.
		h.cronFireRunsMu.Lock()
		isCronFireTurn := h.cronFireRuns[payload.RunID]
		h.cronFireRunsMu.Unlock()
		isChannelTurn := payload.Data.Phase == "start" && payload.RunID != "" &&
			!isDeviceOutboundChatRunID(payload.RunID) && !isDeviceOutboundChatRunID(flowRunID) &&
			!isCronFireTurn
		if isChannelTurn {
			// Neutral placeholder; the goroutine below relabels it once chat.history reveals the source.
			flow.Log("chat_input", map[string]any{"run_id": payload.RunID, "source": "channel"}, payload.RunID)
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "chat_input",
				Summary: "[chat]",
				RunID:   payload.RunID,
				Detail:  map[string]string{"role": "user"},
			})

			// Separate goroutine: FetchChatHistory would deadlock the WS read loop.
			capturedRunID := payload.RunID
			capturedSessionKey := payload.SessionKey
			go func() {
				historyPayload, histErr := h.agentGateway.FetchChatHistory(capturedSessionKey, 20)
				if histErr != nil {
					slog.Warn("chat.history fetch failed (best-effort)", "component", "agent", "run_id", capturedRunID, "err", histErr)
					return
				}
				if historyPayload == nil {
					return
				}
				slog.Info("chat.history for channel turn", "component", "agent", "run_id", capturedRunID, "history_bytes", len(historyPayload))
				if len(historyPayload) < 8000 {
					slog.Info("chat.history raw payload", "component", "agent", "run_id", capturedRunID, "payload", string(historyPayload))
				}

				userMsg, senderLabel, msgTime := extractLastUserMessageFromHistory(historyPayload)
				// Staleness gate: a self-fired run (heartbeat) has no fresh user message, so the fetch
				// would land on the previous turn's. Zero msgTime is treated as fresh.
				if !msgTime.IsZero() && time.Since(msgTime) > 120*time.Second {
					slog.Info("chat.history last user message is stale — not attributing to this run",
						"component", "agent", "run_id", capturedRunID, "msg_age_s", int(time.Since(msgTime).Seconds()))
					return
				}
				// Confirmed channel run; guards the race where a Telegram UUID maps to a sensing trace.
				if senderLabel != "" {
					h.channelRunsMu.Lock()
					h.channelRuns[capturedRunID] = true
					h.channelRunsMu.Unlock()
				}
				if userMsg != "" {
					// Legacy music-proactive cron turns; remove once old crons are cleaned up.
					if strings.Contains(userMsg, "[music-proactive]") {
						resolved := h.resolveRunID(capturedRunID)
						h.agentGateway.MarkBroadcastRun(resolved)
					}

					displayMsg := userMsg
					if len(displayMsg) > 200 {
						displayMsg = displayMsg[:200] + "…"
					}
					// Label: real sender → [channel:sender]; device-internal merge → [voice]/[emotion]/...; else [chat].
					chName := h.agentGateway.GetConfiguredChannel()
					var prefix string
					switch {
					case senderLabel != "":
						prefix = "[" + chName + ":" + senderLabel + "]"
					default:
						if lbl := labelForDeviceInternal(userMsg); lbl != "" {
							prefix = lbl
						} else {
							prefix = "[chat]"
						}
					}
					flow.Log("chat_input", map[string]any{
						"run_id":  capturedRunID,
						"source":  "channel",
						"message": userMsg,
						"sender":  senderLabel,
					}, capturedRunID)
					h.monitorBus.Push(domain.MonitorEvent{
						Type:    "chat_input",
						Summary: prefix + " " + displayMsg,
						RunID:   capturedRunID,
						Detail:  map[string]string{"role": "user", "message": userMsg, "sender": senderLabel},
					})
				}
			}()
		}

		// Busy-gate only device-initiated turns: heartbeat/channel/cron lifecycles can drop their
		// `end` and strand activeTurn, and OpenClaw steer mode batches sensing into them anyway.
		if payload.Data.Phase == "start" {
			deviceInitiated := isDeviceOutboundChatRunID(payload.RunID) || isDeviceOutboundChatRunID(flowRunID)
			if deviceInitiated {
				h.agentGateway.SetBusy(true)
			} else {
				slog.Info("lifecycle.start skipped for busy gating",
					"component", "agent", "run_id", payload.RunID, "flow_run_id", flowRunID,
					"reason", "not device-initiated — heartbeat/channel/cron handled by OpenClaw steer batching")
			}
			// Arm the dead-air filler; no-op unless sensing marked this a voice run.
			sensinghttp.DefaultFillerManager.OnTurnStart(flowRunID)
		} else if payload.Data.Phase == "end" || payload.Data.Phase == "error" {
			h.agentGateway.SetBusy(false)
			// Error skips the lifecycle-end Cancel below, so clean filler state here.
			if payload.Data.Phase == "error" {
				sensinghttp.DefaultFillerManager.Cancel(flowRunID)
				// Error also skips the Slack finalize below; DeliverSlackReply("") only cleans up.
				if sb, ok := h.agentGateway.(domain.SlackBridge); ok {
					go func() {
						if err := sb.DeliverSlackReply(flowRunID, ""); err != nil {
							slog.Error("slack cleanup on error failed", "component", "agent", "err", err)
						}
					}()
				}
			}
		}

		// Token usage: lifecycle_end payload first, fallback to chat.history.
		if payload.Data.Phase == "end" {
			slog.Info("lifecycle end raw", "component", "agent", "runId", payload.RunID, "raw", string(evt.Payload))
			if u := payload.Data.Usage; u != nil {
				slog.Info("token usage", "component", "agent", "runId", payload.RunID,
					"input", u.InputTokens, "output", u.OutputTokens,
					"cacheRead", u.CacheReadTokens, "cacheWrite", u.CacheWriteTokens,
					"total", u.TotalTokens)
				flow.Log("token_usage", map[string]any{
					"run_id":             flowRunID,
					"input_tokens":       u.InputTokens,
					"output_tokens":      u.OutputTokens,
					"cache_read_tokens":  u.CacheReadTokens,
					"cache_write_tokens": u.CacheWriteTokens,
					"total_tokens":       u.TotalTokens,
				}, flowRunID)
				h.monitorBus.Push(domain.MonitorEvent{
					Type:    "token_usage",
					Summary: fmt.Sprintf("in:%d out:%d total:%d", u.InputTokens, u.OutputTokens, u.TotalTokens),
					RunID:   flowRunID,
					Detail: map[string]string{
						"input_tokens":       fmt.Sprintf("%d", u.InputTokens),
						"output_tokens":      fmt.Sprintf("%d", u.OutputTokens),
						"cache_read_tokens":  fmt.Sprintf("%d", u.CacheReadTokens),
						"cache_write_tokens": fmt.Sprintf("%d", u.CacheWriteTokens),
						"total_tokens":       fmt.Sprintf("%d", u.TotalTokens),
					},
				})

				h.maybeAutoNewSession(h.agentGateway.GetSessionKey(), u.TotalTokens, flowRunID)
			} else {
				// OpenClaw lifecycle_end has no usage; fetch from chat.history.
				capturedFlowRunID := flowRunID
				capturedSessionKey := payload.SessionKey
				go func() {
					type histUsage struct {
						Input       int `json:"input"`
						Output      int `json:"output"`
						TotalTokens int `json:"totalTokens"`
						CacheRead   int `json:"cacheRead"`
						CacheWrite  int `json:"cacheWrite"`
					}
					type histContent struct {
						Type     string `json:"type"`
						Text     string `json:"text,omitempty"`
						Thinking string `json:"thinking,omitempty"`
					}
					// Reply persistence races lifecycle end: skip assistant messages older than staleAfter so a
					// prior turn's usage isn't attributed here. Unparseable timestamps count as fresh.
					const staleAfter = 30 * time.Second
					// Retry up to 3 times, 2s apart (WS hiccup or persistence race).
					for attempt := 0; attempt < 3; attempt++ {
						if attempt > 0 {
							time.Sleep(2 * time.Second)
						}
						histPayload, err := h.agentGateway.FetchChatHistory(capturedSessionKey, 5)
						if err != nil {
							slog.Warn("chat.history usage fetch failed — retrying", "component", "agent", "run_id", capturedFlowRunID, "attempt", attempt, "err", err)
							continue
						}
						if histPayload == nil {
							// nil without error: runtime has no walkable history (codex, claudecode); don't retry.
							slog.Debug("chat.history not supported by this runtime — skipping usage attribution", "component", "agent", "run_id", capturedFlowRunID)
							return
						}
						// RawMessage: chat.history mixes string and block-array content, and a typed slice
						// would fail the whole unmarshal.
						var hist struct {
							Messages []struct {
								Role      string          `json:"role"`
								Timestamp json.RawMessage `json:"timestamp,omitempty"`
								Usage     *histUsage      `json:"usage,omitempty"`
								Content   json.RawMessage `json:"content,omitempty"`
							} `json:"messages"`
						}
						if uerr := json.Unmarshal(histPayload, &hist); uerr != nil {
							slog.Warn("chat.history usage payload unmarshal failed — retrying", "component", "agent", "run_id", capturedFlowRunID, "attempt", attempt, "err", uerr)
							continue
						}
						// Only the newest assistant message can be this run's reply.
						idx := -1
						for i := len(hist.Messages) - 1; i >= 0; i-- {
							if hist.Messages[i].Role == "assistant" {
								idx = i
								break
							}
						}
						if idx < 0 {
							continue
						}
						if ts := parseHistoryTimestamp(hist.Messages[idx].Timestamp); !ts.IsZero() && time.Since(ts) > staleAfter {
							slog.Info("chat.history last assistant message is stale — retrying",
								"component", "agent", "run_id", capturedFlowRunID, "attempt", attempt)
							continue
						}
						if hist.Messages[idx].Usage == nil {
							continue
						}
						// Thinking only from the accepted fresh message.
						var contentBlocks []histContent
						_ = json.Unmarshal(hist.Messages[idx].Content, &contentBlocks)
						for _, c := range contentBlocks {
							if c.Type == "thinking" && c.Thinking != "" {
								flow.Log("agent_thinking", map[string]any{
									"run_id": capturedFlowRunID,
									"source": "chat_history",
									"text":   c.Thinking,
								}, capturedFlowRunID)
								h.monitorBus.Push(domain.MonitorEvent{
									Type:    "thinking",
									Summary: c.Thinking,
									RunID:   capturedFlowRunID,
								})
							}
						}
						{
							u := hist.Messages[idx].Usage
							slog.Info("token usage (from chat.history)", "component", "agent",
								"run_id", capturedFlowRunID,
								"input", u.Input, "output", u.Output,
								"cacheRead", u.CacheRead, "cacheWrite", u.CacheWrite,
								"total", u.TotalTokens)
							flow.Log("token_usage", map[string]any{
								"run_id":             capturedFlowRunID,
								"source":             "chat_history",
								"input_tokens":       u.Input,
								"output_tokens":      u.Output,
								"cache_read_tokens":  u.CacheRead,
								"cache_write_tokens": u.CacheWrite,
								"total_tokens":       u.TotalTokens,
							}, capturedFlowRunID)
							h.monitorBus.Push(domain.MonitorEvent{
								Type:    "lifecycle",
								Summary: fmt.Sprintf("Agent end — tokens: %d in / %d out", u.Input, u.Output),
								RunID:   capturedFlowRunID,
								Detail: map[string]string{
									"inputTokens":  fmt.Sprintf("%d", u.Input),
									"outputTokens": fmt.Sprintf("%d", u.Output),
									"cacheRead":    fmt.Sprintf("%d", u.CacheRead),
									"cacheWrite":   fmt.Sprintf("%d", u.CacheWrite),
									"totalTokens":  fmt.Sprintf("%d", u.TotalTokens),
								},
							})
							h.monitorBus.Push(domain.MonitorEvent{
								Type:    "token_usage",
								Summary: fmt.Sprintf("in:%d out:%d total:%d", u.Input, u.Output, u.TotalTokens),
								RunID:   capturedFlowRunID,
								Detail: map[string]string{
									"input_tokens":       fmt.Sprintf("%d", u.Input),
									"output_tokens":      fmt.Sprintf("%d", u.Output),
									"cache_read_tokens":  fmt.Sprintf("%d", u.CacheRead),
									"cache_write_tokens": fmt.Sprintf("%d", u.CacheWrite),
									"total_tokens":       fmt.Sprintf("%d", u.TotalTokens),
								},
							})

							// Auto-new-session: drops in-session history, keeps device external memory.
							h.maybeAutoNewSession(h.agentGateway.GetSessionKey(), u.TotalTokens, capturedFlowRunID)
							return
						}
					}
					slog.Warn("chat.history usage: no fresh assistant message with usage — skipping attribution",
						"component", "agent", "run_id", capturedFlowRunID)
				}()
			}
		}

		// Salvage OpenClaw "incomplete turn" misfires (openclaw#68076/#67855) before emitting the
		// lifecycle event. Sync on the WS worker so wasErrorRecovered is set before the chat error.
		errorRecovered := false
		if payload.Data.Phase == "error" && !strings.HasPrefix(payload.Data.Error, domain.RunExpiryErrorPrefix) {
			errorRecovered = h.tryRecoverIncompleteTurn(payload.RunID, flowRunID, payload.SessionKey)
			if errorRecovered {
				h.markErrorRecovered(flowRunID)
			}
		}
		shortErr := shortError(payload.Data.Error)
		lcData := map[string]any{"run_id": flowRunID, "error": payload.Data.Error}
		if errorRecovered {
			// Blank error so the monitor shows no error node; keep the original for observability.
			lcData["error"] = ""
			lcData["recovered"] = true
			lcData["original_error"] = payload.Data.Error
		}
		if payload.Data.Phase == "start" {
			// Memory fingerprint (sizes + sha8, no content) to tie routing regressions to memory writes.
			if st := migratepersona.MemoryState(); st != nil {
				lcData["memory"] = st
			}
		}
		flow.Log("lifecycle_"+payload.Data.Phase, lcData, flowRunID)
		taskRunID := h.resolveTaskRunID(payload.RunID, flowRunID)
		switch payload.Data.Phase {
		case "end":
			telemetry.ReportTaskLifecycleEnd(taskRunID, payload.Data.Aborted, payload.Data.Error != "")
		case "error":
			if errorRecovered {
				// A salvaged reply does not establish that execution finished.
				telemetry.ReportTaskExecution(taskRunID, "", "unknown", "lifecycle_error_recovered")
			} else {
				telemetry.ReportTaskExecution(taskRunID, "", "failed", "lifecycle_error")
			}
		}
		monEvt := domain.MonitorEvent{
			Type:    "lifecycle",
			Summary: fmt.Sprintf("Agent %s", payload.Data.Phase),
			RunID:   flowRunID,
			Phase:   payload.Data.Phase,
			Error:   shortErr,
		}
		if payload.Data.Phase == "error" && shortErr != "" {
			if errorRecovered {
				monEvt.Error = ""
				monEvt.Summary = "Agent error — reply recovered"
			} else {
				monEvt.Summary = "❌ " + shortErr
			}
		}
		if payload.Data.Phase == "end" && payload.Data.Usage != nil {
			u := payload.Data.Usage
			monEvt.Detail = map[string]string{
				"inputTokens":  fmt.Sprintf("%d", u.InputTokens),
				"outputTokens": fmt.Sprintf("%d", u.OutputTokens),
				"cacheRead":    fmt.Sprintf("%d", u.CacheReadTokens),
				"cacheWrite":   fmt.Sprintf("%d", u.CacheWriteTokens),
				"totalTokens":  fmt.Sprintf("%d", u.TotalTokens),
			}
			monEvt.Summary = fmt.Sprintf("Agent end — tokens: %d in / %d out", u.InputTokens, u.OutputTokens)
		}
		h.monitorBus.Push(monEvt)

		// Clear trace only after end so the UUID → device run mapping still succeeds mid-turn.
		if payload.Data.Phase == "end" || payload.Data.Phase == "error" {
			flow.ClearTrace()
		}

	case "tool":
		toolName := payload.ToolName()
		toolArgs := payload.ToolArguments()
		summary := toolName
		if payload.Data.Phase == "start" {
			// HW-reaction tools soft-cancel pending filler; other tools keep the timer running.
			sensinghttp.DefaultFillerManager.OnToolStart(flowRunID, toolArgs, toolName)
			summary = fmt.Sprintf("Tool %s started", toolName)
			h.rememberToolArgs(payload.Data.ToolCallID, toolArgs)
			// Text streamed before a tool call is narration: move it to the thinking row.
			if narration := h.demoteAssistantBufferToThinking(payload.RunID); narration != "" {
				slog.Info("assistant text before tool call demoted to thinking",
					"component", "agent", "run_id", flowRunID, "tool", toolName,
					"text", narration[:min(len(narration), 120)])
				h.monitorBus.Push(domain.MonitorEvent{Type: "thinking", Summary: narration, RunID: flowRunID})
				flow.Log("narration_demoted", map[string]any{"run_id": flowRunID, "tool": toolName, "text": narration}, flowRunID)
			}
			// The agent sometimes echoes an [HW:...] marker in a shell call, which never reaches HAL;
			// fire it for real and skip the cosmetic detection below to avoid duplicate nodes.
			echoedHW := h.fireEchoedHWMarkers(toolName, toolArgs, flowRunID)
			// Monitor-only: don't suppress TTS here; music_service waits for TTS before taking ALSA.
			if !echoedHW && strings.Contains(toolArgs, "/audio/play") {
				h.monitorBus.Push(domain.MonitorEvent{Type: "hw_audio", Summary: toolArgs, RunID: flowRunID})
				flow.Log("hw_audio", map[string]any{"args": toolArgs, "run_id": flowRunID}, flowRunID)
			}
			// HW events for the flow monitor. /emotion and servo match the resolved path; /led and
			// /audio still use substring matching.
			hwPath := hwPathFromToolArgs(toolArgs)
			if !echoedHW && strings.HasPrefix(hwPath, "/emotion") {
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
			if !echoedHW && isServoMovementPath(hwPath) {
				h.monitorBus.Push(domain.MonitorEvent{Type: "hw_servo", Summary: toolArgs, RunID: flowRunID})
				flow.Log("hw_servo", map[string]any{"path": hwPath, "args": toolArgs, "run_id": flowRunID}, flowRunID)
			}
			// Intercept OpenClaw's built-in tts tool (its audio never reaches the speaker); route to HAL.
			if toolName == "tts" {
				if ttsText := extractTTSText(toolArgs); ttsText != "" {
					isChannelRun := isChannelOriginatedRun(payload.RunID, flowRunID)
					isWebChat := h.agentGateway.IsWebChatRun(flowRunID)
					isSilent := h.agentGateway.IsSilentRun(flowRunID)
					slog.Info("intercepted built-in tts tool, routing to HAL", "component", "agent", "run_id", flowRunID, "text", ttsText[:min(len(ttsText), 80)], "channel_run", isChannelRun, "web_chat", isWebChat, "silent", isSilent)
					flow.Log("tts_send", map[string]any{"run_id": flowRunID, "text": ttsText, "source": "tts_tool_intercept"}, flowRunID)
					if !isChannelRun && !isWebChat && !isSilent {
						sensinghttp.DefaultFillerManager.Cancel(flowRunID)
						h.deliverTTS(h.agentGateway.SendToHALTTS, ttsText, flowRunID, "TTS intercept delivery failed")
					}
					// Mark spoken so lifecycle end doesn't double-speak.
					h.suppressTTS(payload.RunID, "already_spoken")
				}
			}
		} else if payload.Data.Phase == "end" || payload.Data.Phase == "result" {
			// Re-arm the filler after each tool. OpenClaw emits "result" for native tools and "end"
			// for legacy paths; both mark the boundary.
			sensinghttp.DefaultFillerManager.OnToolEnd(flowRunID)
			result := payload.ResultText()
			if len(result) > 100 {
				result = result[:100] + "..."
			}
			summary = fmt.Sprintf("Tool %s done", toolName)
			if result != "" {
				summary += ": " + result
			}
		}
		toolFlowData := map[string]any{"tool": toolName, "phase": payload.Data.Phase, "run_id": flowRunID, "args": toolArgs}
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

	case "thinking":
		delta := payload.Data.Delta
		if delta == "" {
			delta = payload.Data.Text
		}
		if delta != "" {
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "thinking",
				Summary: delta,
				RunID:   flowRunID,
			})
			if h.recordThinkingDelta(flowRunID, delta) {
				flow.Log("thinking_first_token", map[string]any{
					"run_id": flowRunID,
				}, flowRunID)
			}
		}

	case "assistant":
		delta := payload.Data.Delta
		if delta == "" {
			delta = payload.Data.Text
		}
		if delta != "" {
			// Suspend fillers during text; only a new tool.start resumes. End/error still hard-cancel.
			sensinghttp.DefaultFillerManager.OnAssistantText(flowRunID)
			// Once the turn is handed to Harness its own reply (usually NO_REPLY) is deferred,
			// so its text must not stream into chat either.
			if !h.suppressHarnessAgentReply(flowRunID) {
				h.monitorBus.Push(domain.MonitorEvent{
					Type:    "assistant_delta",
					Summary: delta,
					RunID:   flowRunID,
				})
			}
			if h.recordAssistantDelta(flowRunID, delta) {
				flow.Log("agent_first_token", map[string]any{
					"run_id": flowRunID,
				}, flowRunID)
			}
		}

		// Accumulate deltas per run; spoken at lifecycle end.
		h.accumulateAssistantDelta(payload.RunID, delta)

		// Slack bridge: stream the cleaned cumulative text into the live Slack message.
		if sb, ok := h.agentGateway.(domain.SlackBridge); ok && sb.IsSlackOriginRun(flowRunID) {
			if clean, ready := h.cleanedSlackStreamText(payload.RunID); ready {
				sb.StreamSlackDelta(flowRunID, clean)
			}
		}

		// Stream only the FIRST sentence early; lifecycle end queues the remainder via
		// /voice/speak-queue (per-sentence POSTs would add TTFB gaps).
		if h.canStreamSentenceTTS(payload.RunID, flowRunID) {
			if sentence := h.tryFirstSentenceFlush(payload.RunID); sentence != "" {
				cleaned := sanitizeAgentText(sentence)
				if cleaned != "" {
					readyAt := time.Now()
					firstDeltaAt := h.assistantFirstDeltaAt(flowRunID)
					if !firstDeltaAt.IsZero() {
						slog.Info("[tts-timing] sentence_ready", "run_id", flowRunID,
							"text_key", ttsTextKey(cleaned), "first_delta_to_ready_ms", readyAt.Sub(firstDeltaAt).Milliseconds())
					}
					// Fire leading HW markers sync before the TTS POST so state changes (e.g. /scene/off
					// unmuting the speaker) apply first. Inline markers stay deferred to lifecycle end.
					h.assistantMu.Lock()
					buf := h.assistantBuf[payload.RunID]
					rawSnapshot := ""
					if buf != nil {
						rawSnapshot = buf.String()
					}
					h.assistantMu.Unlock()
					if rawSnapshot != "" {
						leading := extractLeadingHWCalls(rawSnapshot)
						if len(leading) > 0 {
							h.fireHWCallsSync(leading, flowRunID)
							h.recordFiredHWCount(payload.RunID, len(leading))
						}
					}
					slog.Info("streaming first sentence to TTS",
						"component", "agent",
						"run_id", flowRunID,
						"sentence", cleaned[:min(len(cleaned), 100)])
					flow.Log("tts_stream_send", map[string]any{"run_id": flowRunID, "text": cleaned}, flowRunID)
					// Real speech ends filler eligibility even if more tools follow.
					sensinghttp.DefaultFillerManager.Cancel(flowRunID)
					slog.Info("[tts-timing] sentence_dispatch", "run_id", flowRunID,
						"text_key", ttsTextKey(cleaned), "ready_to_dispatch_ms", time.Since(readyAt).Milliseconds())
					h.deliverTTSQueue(cleaned, flowRunID, "streaming TTS delivery failed")
				}
			}
		}

	}

	// Lifecycle end: flush accumulated assistant text to TTS unless suppressed.
	if payload.Stream == "lifecycle" && payload.Data.Phase == "end" {
		lifecycleEndAt := time.Now()
		// Persist stream summaries to JSONL; raw deltas only live in monitorBus (RAM).
		if s := h.drainStreamStats(flowRunID); s != nil {
			if s.thinkingChunks > 0 {
				flow.Log("thinking_last_token", map[string]any{
					"run_id": flowRunID,
					"text":   s.thinkingText.String(),
					"chunks": s.thinkingChunks,
					"chars":  s.thinkingChars,
				}, flowRunID)
			}
			if s.assistantChunks > 0 {
				lastToken := map[string]any{
					"run_id": flowRunID, "text": s.assistantText.String(),
					"chunks": s.assistantChunks, "chars": s.assistantChars,
				}
				if elapsed, known := s.assistantElapsedMs(lifecycleEndAt); known {
					lastToken["first_delta_to_end_ms"] = elapsed
					slog.Info("[tts-timing] assistant_end", "run_id", flowRunID,
						"first_delta_to_end_ms", elapsed)
				}
				flow.Log("agent_last_token", lastToken, flowRunID)
			}
		}

		// Hard-cancel filler before the real flush (covers NO_REPLY / HW-only / error).
		sensinghttp.DefaultFillerManager.Cancel(flowRunID)
		suppressReason := h.clearTTSSuppress(payload.RunID)
		// Consume up front so the entry is cleared on every branch.
		interleavedDMTarget := h.consumeInterleavedDM(payload.RunID)
		if interleavedDMTarget == "" {
			interleavedDMTarget = h.consumeInterleavedDM(flowRunID)
		}
		// Web monitor chat: suppress TTS — response displayed in web UI only.
		if suppressReason == "" && h.agentGateway.ConsumeWebChatRun(flowRunID) {
			suppressReason = "web_chat"
		}
		// Realtime voice agent already spoke this turn.
		if suppressReason == "" && h.agentGateway.ConsumeSilentRun(flowRunID) {
			suppressReason = "voice_agent_handled"
		}
		text, hwCalls := h.flushAssistantText(payload.RunID)
		finalBufferReadyAt := time.Now()
		// Non-zero when sentence 1 was streamed mid-turn; the remainder POST skips it.
		streamedLen := h.consumeStreamedCleanLen(payload.RunID)
		if streamedLen > len(text) {
			streamedLen = len(text)
		}
		streamed := streamedLen > 0
		// Skip leading HW markers already fired at stream time (order is stable).
		firedAtStream := h.consumeFiredHWCount(payload.RunID)
		if firedAtStream > len(hwCalls) {
			firedAtStream = len(hwCalls)
		}
		hwCalls = hwCalls[firedAtStream:]
		if text != "" || len(hwCalls) > 0 || streamed {
			// Sync so state-changing markers (e.g. /scene/off) apply before the TTS POST;
			// fireHWCallsSync has a 100ms per-call timeout with async fallback.
			h.fireHWCallsSync(hwCalls, flowRunID)

			// Consume early to avoid a map leak on NO_REPLY/empty/suppressed paths.
			isBroadcastRun := h.agentGateway.ConsumeBroadcastRun(flowRunID)

			// Peek only; DeliverSlackReply consumes at reply time. False for non-SlackBridge runtimes.
			slackBridge, _ := h.agentGateway.(domain.SlackBridge)
			isSlackRun := slackBridge != nil && slackBridge.IsSlackOriginRun(flowRunID)

			// Markers: /broadcast fans out to Telegram, /speak forces TTS, /dm targets one Telegram user.
			var dmTelegramID string
			forceTTS := false
			for _, c := range hwCalls {
				if c.path == "/broadcast" {
					isBroadcastRun = true
				}
				if c.path == "/speak" {
					forceTTS = true
				}
				if c.path == "/dm" {
					var dm struct {
						TelegramID string `json:"telegram_id"`
					}
					if err := json.Unmarshal([]byte(c.body), &dm); err == nil && dm.TelegramID != "" {
						dmTelegramID = dm.TelegramID
					}
				}
			}
			// No /dm marker but a Telegram message was interleaved mid-turn: reply to that chat.
			if dmTelegramID == "" && interleavedDMTarget != "" {
				dmTelegramID = interleavedDMTarget
				slog.Info("routing reply to interleaved Telegram chat (queue-mode injection)",
					"component", "agent", "run_id", flowRunID, "chat_id", dmTelegramID)
			}

			// Guard mode alerts the owner even on NO_REPLY / empty / suppressed paths.
			if snap, ok := h.agentGateway.ConsumeGuardRun(flowRunID); ok {
				guardText := text
				if guardText == "" || isAgentNoReply(guardText) {
					guardText = "Motion or presence detected while guard mode is active."
				}
				go func(t, s string) {
					slog.Info("guard broadcast via Telegram Bot API", "component", "agent", "run_id", flowRunID, "text", t[:min(len(t), 80)])
					if err := h.agentGateway.Broadcast(t, s); err != nil {
						slog.Error("guard broadcast failed", "component", "agent", "err", err)
					}
				}(guardText, snap)
			}

			// Detect heartbeat before sanitizing strips the sentinel.
			isHeartbeatRun := strings.Contains(strings.ToUpper(text), "HEARTBEAT_OK")
			if isHeartbeatRun {
				flow.Log("heartbeat_run", map[string]any{"run_id": flowRunID}, flowRunID)
				h.monitorBus.Push(domain.MonitorEvent{
					Type:    "heartbeat_run",
					Summary: "OpenClaw heartbeat",
					RunID:   flowRunID,
				})
			}
			// Unwrap <say>...</say> if present.
			text = extractSayTag(text)
			text = sanitizeAgentText(text)
			// Skip the already-streamed prefix; clamp since sanitizing may shorten text.
			if streamedLen > len(text) {
				streamedLen = len(text)
			}
			remainderText := strings.TrimSpace(text[streamedLen:])
			// Drop CoT planning leaks (cot_leak_filter.go). The remainder filter is seeded with the
			// streamed prefix; `text` is replaced only when something was dropped.
			cotLang := h.replyLanguageCode()
			fullFilter := newCoTLeakFilter(cotLang)
			if filteredFull := fullFilter.filterText(text); len(fullFilter.dropped) > 0 {
				rf := newCoTLeakFilter(cotLang)
				rf.filterText(text[:streamedLen]) // seed only; drops already handled at stream time
				rf.dropped = nil
				remainderText = strings.TrimSpace(rf.filterText(remainderText))
				slog.Warn("CoT leak dropped from agent reply",
					"component", "agent", "run_id", flowRunID,
					"dropped", len(fullFilter.dropped),
					"before_len", len(text), "after_len", len(filteredFull),
					"preview", cotDroppedPreview(fullFilter.dropped, 200))
				flow.Log("cot_leak_filtered", map[string]any{
					"run_id":  flowRunID,
					"dropped": len(fullFilter.dropped),
					"preview": cotDroppedPreview(fullFilter.dropped, 500),
				}, flowRunID)
				text = filteredFull
			}
			if h.suppressHarnessAgentReply(flowRunID) {
				slog.Info("agent deferred reply to Harness", "component", "agent", "run_id", flowRunID)
				flow.Log("harness_reply_pending", map[string]any{"run_id": flowRunID}, flowRunID)
				h.ResumeHarnessVoiceFillers(flowRunID)
				return nil
			}
			if isAgentNoReply(text) || isMetaNonReply(text) {
				// Sentence 1 may already have been spoken and can't be unspoken.
				if streamed {
					slog.Warn("NO_REPLY in remainder after first sentence streamed",
						"component", "agent", "run_id", flowRunID, "streamed_len", streamedLen)
				} else {
					slog.Info("agent replied NO_REPLY, skipping TTS", "component", "agent", "run_id", flowRunID)
				}
				flow.Log("no_reply", map[string]any{"run_id": flowRunID}, flowRunID)
				h.monitorBus.Push(domain.MonitorEvent{
					Type:    "chat_response",
					Summary: "[no reply]",
					RunID:   flowRunID,
					State:   "final",
					Detail:  map[string]string{"role": "assistant", "message": "[no reply]"},
				})
			} else if remainderText == "" {
				h.clearHarnessResponseRun(flowRunID)
				if streamed {
					slog.Info("assistant turn complete via first-sentence streaming",
						"component", "agent", "run_id", flowRunID, "streamed_len", streamedLen)
					flow.Log("tts_stream_complete", map[string]any{"run_id": flowRunID, "text": text}, flowRunID)
				} else {
					flow.Log("hw_only_reply", map[string]any{"run_id": flowRunID}, flowRunID)
				}
			} else if suppressReason != "" {
				h.clearHarnessResponseRun(flowRunID)
				slog.Info("assistant turn done, TTS suppressed", "component", "agent", "reason", suppressReason, "text", text[:min(len(text), 100)])
				flow.Log("tts_suppressed", map[string]any{"run_id": flowRunID, "reason": suppressReason, "text": text}, flowRunID)
			} else {
				h.clearHarnessResponseRun(flowRunID)
				// Channel detection is positive-evidence only: tg- runs or runs marked in channelRuns.
				isChannelRun := isChannelOriginatedRun(payload.RunID, flowRunID)
				// Cron-fire turns always speak even though their UUIDs look like channel runs.
				h.cronFireRunsMu.Lock()
				isCronFire := h.cronFireRuns[payload.RunID] || h.cronFireRuns[flowRunID]
				delete(h.cronFireRuns, payload.RunID)
				delete(h.cronFireRuns, flowRunID)
				h.cronFireRunsMu.Unlock()
				if isCronFire {
					isChannelRun = false
				}
				// [HW:/broadcast] (guard) or [HW:/speak] (proactive crons) force TTS
				// even for channel-origin runs.
				if isBroadcastRun || forceTTS {
					isChannelRun = false
				}
				// Heartbeat cron responses must never reach the speaker.
				if isHeartbeatRun {
					isChannelRun = true
				}
				// A confirmed channel turn (senderLabel) always suppresses TTS.
				h.channelRunsMu.Lock()
				if h.channelRuns[payload.RunID] || h.channelRuns[flowRunID] {
					isChannelRun = true
				}
				delete(h.channelRuns, payload.RunID)
				delete(h.channelRuns, flowRunID)
				h.channelRunsMu.Unlock()
				// A Slack-origin turn replies in Slack, never on the speaker.
				if isSlackRun {
					isChannelRun = true
				}
				if isChannelRun {
					// Channel users still get the text via OpenClaw's own fan-out.
					slog.Info("assistant turn done, TTS suppressed (channel run)", "component", "agent", "text", text[:min(len(text), 100)], "broadcast", isBroadcastRun, "force_tts", forceTTS, "cron_fire", isCronFire, "heartbeat", isHeartbeatRun)
					flow.Log("tts_suppressed", map[string]any{"run_id": flowRunID, "reason": "channel_run", "text": text}, flowRunID)
				} else {
					// speak-queue pre-synthesises the remainder while sentence 1 plays; idle it acts like /voice/speak.
					slog.Info("assistant turn done, sending to TTS",
						"component", "agent",
						"text", remainderText[:min(len(remainderText), 100)],
						"streamed_len", streamedLen,
						"broadcast", isBroadcastRun, "force_tts", forceTTS,
						"cron_fire", isCronFire, "heartbeat", isHeartbeatRun)
					// full_text lets the web show the complete reply (sentence 1 was logged as tts_stream_send).
					flow.Log("tts_send", map[string]any{"run_id": flowRunID, "text": remainderText, "full_text": text, "streamed_len": streamedLen}, flowRunID)
					slog.Info("[tts-timing] final_dispatch", "run_id", flowRunID,
						"text_key", ttsTextKey(remainderText), "streamed_len", streamedLen,
						"end_to_buffer_ready_ms", finalBufferReadyAt.Sub(lifecycleEndAt).Milliseconds(),
						"buffer_ready_to_dispatch_ms", time.Since(finalBufferReadyAt).Milliseconds())
					h.deliverTTSQueue(remainderText, flowRunID, "TTS delivery failed")
				}
				// /dm takes priority over /broadcast.
				if dmTelegramID != "" && len(text) > 10 {
					// Attach the worst pose frames when a posture nudge triggered this turn.
					poseBucket, poseFiles, hasPoseBucket := h.agentGateway.ConsumePoseBucketRun(flowRunID)
					var poseImagePaths []string
					if hasPoseBucket {
						poseImagePaths = buildPoseBucketImagePaths(poseBucket, poseFiles)
						slog.Info("dm attaching pose bucket images",
							"component", "agent", "run_id", flowRunID,
							"bucket", poseBucket, "count", len(poseImagePaths))
					}
					go func(t, tid string, paths []string) {
						slog.Info("dm run response to user", "component", "agent", "run_id", flowRunID, "telegram_id", tid, "text", t[:min(len(t), 80)], "images", len(paths))
						if len(paths) > 0 {
							if err := h.agentGateway.SendToUserWithMedia(tid, t, paths); err != nil {
								slog.Error("dm run with media failed", "component", "agent", "err", err)
							}
							return
						}
						if err := h.agentGateway.SendToUser(tid, t, ""); err != nil {
							slog.Error("dm run failed", "component", "agent", "err", err)
						}
					}(text, dmTelegramID, poseImagePaths)
				} else if isBroadcastRun && len(text) > 10 {
					// Broadcast run (e.g. music.mood): send the reply to all channels.
					go func(t string) {
						slog.Info("broadcast run response to channels", "component", "agent", "run_id", flowRunID, "text", t[:min(len(t), 80)])
						if err := h.agentGateway.Broadcast(t, ""); err != nil {
							slog.Error("broadcast run failed", "component", "agent", "err", err)
						}
					}(text)
				}
			}

			// Finalize Slack on every end outcome so the stream goroutine doesn't leak; NO_REPLY → "".
			if isSlackRun && slackBridge != nil {
				slackText := text
				if isAgentNoReply(text) {
					slackText = ""
				}
				go func(t string) {
					if err := slackBridge.DeliverSlackReply(flowRunID, t); err != nil {
						slog.Error("slack reply failed", "component", "agent", "err", err)
					}
				}(slackText)
			}
		}
	}

	return nil
}
