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

// hwHostRE matches a HAL or os-server endpoint as it appears in a tool call —
// the loopback host and port, then the path. Anchoring on the host is what
// separates a CALL from a MENTION: `cat …/skills/emotion/SKILL.md` contains
// "/emotion", and a plain substring test emitted a phantom hw_emotion + led_set
// for a documentation read (#342 defect D, device-chat-13 seq 109). The monitor
// then showed an emotion the lamp never played.
var hwHostRE = regexp.MustCompile(`127\.0\.0\.1:500[01](/[A-Za-z0-9_./-]*)`)

// hwPathFromToolArgs returns the endpoint path a tool call actually targets, or
// "" when the text merely mentions one. The first call in the text wins. Query
// strings and trailing punctuation are trimmed so callers compare against a
// plain path.
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

// servoMovementPaths are the servo endpoints that MOVE the body. Reads
// (/servo/position, /servo/status, /servo/bearing) are deliberately absent: a
// hw_servo event means the lamp did something a person could see.
//
// /servo/search and /servo/demo were missing from the previous inline list, so
// the one turn that actually swept (device-chat-9, a 42 s curl) showed an idle
// lamp in the monitor while the room was being searched.
var servoMovementPaths = map[string]bool{
	"/servo/aim":    true,
	"/servo/play":   true,
	"/servo/nudge":  true,
	"/servo/search": true,
	"/servo/demo":   true,
}

func isServoMovementPath(path string) bool { return servoMovementPaths[path] }

// handleAgentStreamEvent handles WS event=="agent": the OpenClaw agent stream
// (lifecycle / tool / thinking / assistant) plus the lifecycle-end TTS flush.
// Extracted verbatim from HandleEvent; dispatch lives in handler_events.go.
func (h *AgentHandler) handleAgentStreamEvent(evt domain.WSEvent) error {
	var payload domain.AgentPayload
	if err := json.Unmarshal(evt.Payload, &payload); err != nil {
		return err
	}
	// Capture session key from any agent event
	if payload.SessionKey != "" && h.agentGateway.GetSessionKey() == "" {
		h.agentGateway.SetSessionKey(payload.SessionKey)
	}

	// Map OpenClaw UUID → device idempotencyKey on lifecycle_start.
	// Only map when the lifecycle belongs to the device's own direct session — group/channel
	// sessions have independent runs that must NOT be merged into sensing traces.
	//
	// Two paths depending on payload.RunID format:
	//   • device-format (device-chat-*): OpenClaw 5.4+ echoes the idempotencyKey as
	//     the runId — already IS the device trace. Just remove from pending.
	//   • UUID: produced when OpenClaw drains its followup queue (the
	//     FollowupRun type does not carry idempotencyKey, so
	//     agent-runner-execution.ts mints a fresh UUID at lifecycle time).
	//     Resolve by fetching chat.history and matching the agent's last
	//     user message against the stored pending text. Correct by content
	//     rather than by send-order — drain reordering, dropped turns,
	//     /new session clears, and concurrent channel UUIDs no longer
	//     shift the mapping.
	//
	// This runs synchronously: the WS read loop now dispatches handler
	// events through a worker goroutine (service_ws.go), so chat.history's
	// pendingRPC wait no longer deadlocks against the read loop. Sync map
	// before flowRunID is computed below — every subsequent event for this
	// UUID resolves to the device id from the very first emit, eliminating
	// the split-turn race the previous async version had.
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

	// Resolve OpenClaw UUID → device ID for consistent flow tracing across all agent events
	flowRunID := h.resolveRunID(payload.RunID)
	if payload.Stream == "lifecycle" && (payload.Data.Phase == "end" || payload.Data.Phase == "error") {
		// Register all final reply submissions before releasing processing.
		// The HAL bridge waits for their asynchronous admission callbacks.
		defer hal.EndVoiceFollowup(flowRunID)
	}
	switch payload.Stream {
	case "lifecycle":
		slog.Info("lifecycle event", "component", "agent", "phase", payload.Data.Phase, "runId", payload.RunID, "flowRunId", flowRunID, "session", payload.SessionKey)
		if payload.Data.Phase == "start" {
			h.ttsTurnSequence(flowRunID)
		}

		// Track agent-path activity per sessionKey so the session.message
		// handler can skip turns already driven by the agent stream
		// (cron heartbeat fires both; real user telegram fires only
		// session.message because of OpenClaw's isControlUiVisible gate).
		// Clear on end/error so a subsequent channel turn on the same
		// session within 30s isn't wrongly skipped — the previous turn
		// is finished, agent path is no longer handling anything.
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

		// Correlate with the FIFO queue of recent cron "started" events:
		// the cron event lacks the upcoming runId AND (for sessionTarget=
		// "main" jobs) lacks sessionKey too, so we consume the oldest
		// timestamp within cronFireWindowMs. Restricted to UUID runIds
		// (no device- prefix) so chat.send/sensing turns can't accidentally
		// claim a queued cron slot.
		if payload.Data.Phase == "start" && payload.RunID != "" && !isDeviceOutboundChatRunID(payload.RunID) {
			now := time.Now().UnixMilli()
			cutoff := now - cronFireWindowMs
			h.cronFireExpectedMu.Lock()
			// Drop stale entries from the head.
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
				// Emit a cron_fire flow event so the web monitor can classify
				// this turn as cron without re-deriving via string match on
				// the systemEvent wrapper template.
				flow.Log("cron_fire", map[string]any{"run_id": payload.RunID, "delta_ms": now - startedAt}, payload.RunID)
			} else {
				h.cronFireExpectedMu.Unlock()
			}
		}

		// Detect external channel-initiated turns: lifecycle_start arrives from OpenClaw
		// with a UUID run_id (not device-chat-* prefix). This covers:
		// 1. No active trace (original case)
		// 2. Active trace from a different turn (sensing trace still active when Telegram arrives)
		//
		// Cron-fire turns also have UUID runIds but are NOT channel input —
		// the cron_fire flow event represents them in the monitor, so skip
		// the chat_input emit here to keep the CH IN node from lighting up
		// for scheduled reminders.
		h.cronFireRunsMu.Lock()
		isCronFireTurn := h.cronFireRuns[payload.RunID]
		h.cronFireRunsMu.Unlock()
		isChannelTurn := payload.Data.Phase == "start" && payload.RunID != "" &&
			!isDeviceOutboundChatRunID(payload.RunID) && !isDeviceOutboundChatRunID(flowRunID) &&
			!isCronFireTurn
		if isChannelTurn {
			// Emit chat_input immediately so UI shows turn-started.
			// Use a neutral "[chat]" placeholder rather than claiming the
			// configured channel — the goroutine below will replace this
			// with the right label ([telegram:Gray] / [voice] / [emotion]
			// / ...) once chat.history reveals whether it's a real
			// channel user or a device-internal sensing/voice merge. If
			// the goroutine fails or times out, this generic label
			// stays — better than mis-attributing to Telegram.
			flow.Log("chat_input", map[string]any{"run_id": payload.RunID, "source": "channel"}, payload.RunID)
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "chat_input",
				Summary: "[chat]",
				RunID:   payload.RunID,
				Detail:  map[string]string{"role": "user"},
			})

			// Best-effort: fetch chat history in a separate goroutine to avoid
			// deadlocking the WS read loop (FetchChatHistory waits for a response
			// that can only arrive after this handler returns).
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
				// Dump the last message raw JSON — helps identify a cleaner cron-fire
				// signal (e.g. role:"system", kind:"systemEvent") than string matching.
				// Temporary — remove once schema is confirmed.
				if len(historyPayload) < 8000 {
					slog.Info("chat.history raw payload", "component", "agent", "run_id", capturedRunID, "payload", string(historyPayload))
				}

				userMsg, senderLabel, msgTime := extractLastUserMessageFromHistory(historyPayload)
				// Staleness gate: this fetch races OpenClaw persisting the run's
				// input. A self-fired run (heartbeat) has NO fresh user message, so
				// the fetch lands on the PREVIOUS turn's — emitting it here made the
				// heartbeat render as a doppelgänger of the last real turn (same
				// [activity] text). A genuinely fresh channel message is persisted
				// seconds before lifecycle_start, so a 120s window keeps every real
				// case (steer-merges included) while rejecting minutes-old leftovers.
				// Zero msgTime (no timestamp field) is treated as fresh.
				if !msgTime.IsZero() && time.Since(msgTime) > 120*time.Second {
					slog.Info("chat.history last user message is stale — not attributing to this run",
						"component", "agent", "run_id", capturedRunID, "msg_age_s", int(time.Since(msgTime).Seconds()))
					return
				}
				// Mark as confirmed channel run if a real sender is present.
				// Guards against race: Telegram UUID mapped to sensing trace
				// makes flowRunID = device-sensing-* → isChannelRun wrongly false.
				if senderLabel != "" {
					h.channelRunsMu.Lock()
					h.channelRuns[capturedRunID] = true
					h.channelRunsMu.Unlock()
				}
				// Cron-fire detection happens at lifecycle_start (see correlation
				// against cronFireExpected) — no need to inspect userMsg here.
				if userMsg != "" {
					// Legacy: detect old music-proactive cron turns (before event-driven suggestion).
					// Safe to remove once all devices have been updated and old crons are cleaned up.
					if strings.Contains(userMsg, "[music-proactive]") {
						resolved := h.resolveRunID(capturedRunID)
						h.agentGateway.MarkBroadcastRun(resolved)
					}

					displayMsg := userMsg
					if len(displayMsg) > 200 {
						displayMsg = displayMsg[:200] + "…"
					}
					// Label selection (priority order):
					//  1. Real channel user (senderLabel filled by chat.history) →
					//     `[telegram:Gray]` — keeps existing Telegram UI.
					//  2. device-internal sensing/voice/wellbeing/system message
					//     merged into this UUID turn via OpenClaw steer →
					//     `[voice]` / `[emotion]` / `[activity]` / ... so the
					//     monitor doesn't mis-label self-fire turns as
					//     `[telegram]`.
					//  3. Fallback: generic `[chat]` — UUID with no sender and
					//     no recognisable internal prefix (rare; was previously
					//     mis-labelled as the configured channel).
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

		// Track busy state so passive sensing events can be suppressed during active turns.
		// Only gate on lifecycles that belong to a device-initiated turn — these are
		// the only ones whose `end` is reliably round-tripped through SSE.
		// Heartbeat (target:"none"), channel turns merged by steer mode, and other
		// OpenClaw self-trigger lifecycles can drop their `end` SSE (per the
		// busyTTL comment in service_events.go); gating on them strands activeTurn=true
		// for up to 5 minutes — every device sensing event in that window queues
		// instead of forwarding.
		//
		// External turns don't NEED device-side gating: with messages.queue.mode=steer
		// (pinned in onboarding), concurrent sensing events arriving during a
		// channel/cron turn are batched into the active turn at the next model
		// boundary by OpenClaw itself — no need for the device to pre-suppress them.
		//
		// device-initiated turns also flip activeTurn=true at chat.send time
		// (service_chat.go), so a missed lifecycle.start here is harmless.
		// LED is managed by the agent via /emotion skill calls — do not override here.
		if payload.Data.Phase == "start" {
			deviceInitiated := isDeviceOutboundChatRunID(payload.RunID) || isDeviceOutboundChatRunID(flowRunID)
			if deviceInitiated {
				h.agentGateway.SetBusy(true)
			} else {
				slog.Info("lifecycle.start skipped for busy gating",
					"component", "agent", "run_id", payload.RunID, "flow_run_id", flowRunID,
					"reason", "not device-initiated — heartbeat/channel/cron handled by OpenClaw steer batching")
			}
			// Arm the dead-air filler timer for voice turns. No-op
			// unless sensing handler called MarkVoiceRun(flowRunID)
			// before forwarding this turn.
			sensinghttp.DefaultFillerManager.OnTurnStart(flowRunID)
		} else if payload.Data.Phase == "end" || payload.Data.Phase == "error" {
			h.agentGateway.SetBusy(false)
			// Cancel on error too — lifecycle.end has its own Cancel
			// further down (just before TTS flush), but error skips
			// that block, so clean filler state here.
			if payload.Data.Phase == "error" {
				sensinghttp.DefaultFillerManager.Cancel(flowRunID)
				// The lifecycle-end Slack finalize (further down) is skipped on error,
				// so clean up any Slack stream here. DeliverSlackReply("") consumes the
				// origin, stops the per-run stream goroutine, and posts nothing; it's a
				// no-op for non-Slack runs (and runtimes that aren't a SlackBridge).
				if sb, ok := h.agentGateway.(domain.SlackBridge); ok {
					go func() {
						if err := sb.DeliverSlackReply(flowRunID, ""); err != nil {
							slog.Error("slack cleanup on error failed", "component", "agent", "err", err)
						}
					}()
				}
			}
		}

		// Token usage: try lifecycle_end payload first, fallback to chat.history RPC.
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

				// Auto-new-session parity with the chat.history branch below.
				h.maybeAutoNewSession(h.agentGateway.GetSessionKey(), u.TotalTokens, flowRunID)
			} else {
				// OpenClaw lifecycle_end does not include usage. Fetch from chat.history instead.
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
					// This run's reply is persisted by OpenClaw around the same
					// moment lifecycle end reaches us, so the first fetch can race
					// persistence and see only the PREVIOUS turn's assistant
					// message. Without the freshness check below that stale
					// message's usage (and thinking) were attributed to this run —
					// heartbeat runs cloned the prior turn's token numbers in the
					// Flow monitor. Messages without a parseable timestamp (older
					// gateways) are treated as fresh to keep the old behavior.
					const staleAfter = 30 * time.Second
					// Every non-success path logs and RETRIES (3 attempts, 2s
					// apart): the silent returns this loop replaces were the
					// long-standing "some turns never show tokens" hole — a WS
					// hiccup at boot or a race with reply persistence dropped
					// the attribution with no trace in the journal.
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
							// nil-with-no-error is the deliberate stub signal from
							// runtimes without walkable history (codex, claudecode):
							// no usage to attribute — bail quietly, don't retry.
							slog.Debug("chat.history not supported by this runtime — skipping usage attribution", "component", "agent", "run_id", capturedFlowRunID)
							return
						}
						// Content is RawMessage, NOT []histContent: chat.history mixes
						// shapes per message (plain string for system/user texts,
						// block array for assistant replies). Typing it as a slice
						// made ONE string-content message anywhere in the window fail
						// the WHOLE unmarshal — the long-standing reason some turns
						// never showed tokens (e.g. every turn right after the
						// "[system] wake" message).
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
						// The NEWEST assistant message is the only candidate for this
						// run's reply — walking further back is exactly how a stale
						// turn's usage used to get stolen.
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
						// Thinking is emitted only off the accepted (fresh) message so a
						// stale reply's monologue never replays under this run either.
						// String-content messages simply have no thinking blocks.
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
							// Auto-compact (legacy) — slow but preserves verbatim history
							// via generated summary. Disabled in favour of new-session
							// below; restore by uncommenting if new-session causes memory
							// regressions. See maybeAutoCompact + maybeAutoNewSession in
							// handler_session_lifecycle.go for trade-offs.
							// h.maybeAutoCompact(h.agentGateway.GetSessionKey(), u.TotalTokens, capturedFlowRunID)

							// Auto-new-session — instant, drops in-session conversation
							// history but keeps device external memory (mood/habit/owner).
							h.maybeAutoNewSession(h.agentGateway.GetSessionKey(), u.TotalTokens, capturedFlowRunID)
							return
						}
					}
					slog.Warn("chat.history usage: no fresh assistant message with usage — skipping attribution",
						"component", "agent", "run_id", capturedFlowRunID)
				}()
			}
		}

		// OpenClaw 2026.6.x "incomplete turn" misfire: the gateway surfaces
		// "couldn't generate a response" even though the model replied
		// (streamed deltas or session history; payloads=0 counting bug,
		// openclaw#68076/#67855 family). Salvage the reply BEFORE emitting
		// the lifecycle event so the turn renders as recovered, not failed.
		// Runs sync on the WS worker — the chat-stream error for the same
		// run arrives after this returns, so wasErrorRecovered is reliable.
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
			// Blank the error field so the flow monitor doesn't render an
			// error node over the recovered reply; keep the original for
			// observability.
			lcData["error"] = ""
			lcData["recovered"] = true
			lcData["original_error"] = payload.Data.Error
		}
		if payload.Data.Phase == "start" {
			// Fingerprint of the memory this turn runs with (sizes + sha8, no
			// content) so a routing regression can be tied to a memory write.
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

		// Keep flow.GetTrace() "active" for the duration of the device turn so Telegram heuristic
		// (lifecycle_start arriving while no device trace is active) can work correctly.
		// Clear only after lifecycle_end so openclaw UUID → device runId mapping still succeeds.
		if payload.Data.Phase == "end" || payload.Data.Phase == "error" {
			flow.ClearTrace()
		}

	case "tool":
		toolName := payload.ToolName()
		toolArgs := payload.ToolArguments()
		summary := toolName
		if payload.Data.Phase == "start" {
			// Hardware-reaction tools soft-cancel any pending filler —
			// the user already perceives the device reacting. Non-HW
			// tools leave the timer running so the filler can fire
			// during a long Bash/curl/Read.
			sensinghttp.DefaultFillerManager.OnToolStart(flowRunID, toolArgs, toolName)
			summary = fmt.Sprintf("Tool %s started", toolName)
			h.rememberToolArgs(payload.Data.ToolCallID, toolArgs)
			// Text streamed before this tool call is narration, not the reply
			// — see demoteAssistantBufferToThinking. Keep it visible in the
			// Flow Monitor thinking row, drop it from the reply buffer.
			if narration := h.demoteAssistantBufferToThinking(payload.RunID); narration != "" {
				slog.Info("assistant text before tool call demoted to thinking",
					"component", "agent", "run_id", flowRunID, "tool", toolName,
					"text", narration[:min(len(narration), 120)])
				h.monitorBus.Push(domain.MonitorEvent{Type: "thinking", Summary: narration, RunID: flowRunID})
				flow.Log("narration_demoted", map[string]any{"run_id": flowRunID, "tool": toolName, "text": narration}, flowRunID)
			}
			// DEFENSIVE (2026-07-23): the agent sometimes wraps an [HW:...]
			// marker inside a shell tool call — e.g.
			// `echo '[HW:/audio/play:{...}]'` — instead of emitting it as
			// reply text. A shell echo never reaches the reply-text marker
			// interceptor, so the command no-ops: the flow node lights up
			// (args contain the marker string) but HAL gets nothing and no
			// music plays. Fire the echoed markers for real; when it does,
			// skip the cosmetic-only detection below to avoid duplicate nodes.
			echoedHW := h.fireEchoedHWMarkers(toolName, toolArgs, flowRunID)
			// Detect music playback tool calls (Music skill uses Bash+curl to
			// POST /audio/play) — emit the flow/monitor event only.
			// [2026-06-30] Do NOT suppress TTS here: the agent's reply (e.g.
			// "Chơi nhạc Ghibli tiếp nha!") must speak BEFORE music plays. The
			// Python music_service already waits for TTS via wait_for_tts()
			// before grabbing ALSA, so this Go-side suppress is redundant and
			// was swallowing the spoken reply. Mirrors the 2026-05-11 fix that
			// disabled the hwCalls /audio/play suppress below (this tool-call
			// path was missed then, and re-surfaced in the 2026-06-18 refactor).
			if !echoedHW && strings.Contains(toolArgs, "/audio/play") {
				h.monitorBus.Push(domain.MonitorEvent{Type: "hw_audio", Summary: toolArgs, RunID: flowRunID})
				flow.Log("hw_audio", map[string]any{"args": toolArgs, "run_id": flowRunID}, flowRunID)
				// music.play logged via flow.Log above
			}
			// Emit specific hardware events for flow monitor visualization.
			// Both flow.Log (for JSONL persistence + UI flow_event triggers) and monitorBus (for SSE).
			//
			// Matched on the RESOLVED endpoint path, not on raw shell text. The
			// /led and /audio branches below still use the substring form: same
			// weakness, but no evidence of a phantom there yet, and a change to
			// how they match is a separate decision.
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
			// Intercept OpenClaw built-in tts tool: extract text and route to HAL speaker.
			// The built-in tts generates audio server-side but never reaches the physical speaker.
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
					// Mark this turn as already spoken so lifecycle_end won't double-speak.
					h.suppressTTS(payload.RunID, "already_spoken")
				}
			}
		} else if payload.Data.Phase == "end" || payload.Data.Phase == "result" {
			// Tool finished — re-arm the filler timer if the turn is
			// still active. Long multi-tool turns get a filler at each
			// dead-air pocket, capped by MaxFillersPerTurn and gated
			// by FillerCooldown.
			//
			// OpenClaw emits phase="result" for native tools (read,
			// web_search, web_fetch, exec, …) and phase="end" for
			// some legacy paths; both signal the same boundary. Until
			// 2026-05-12 this branch only matched "end", which meant
			// every native tool silently skipped the filler re-arm
			// and only the very first Continuation ever fired —
			// observable as "no filler during web_search" UX.
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
		// Don't truncate deltas — they are merged in the frontend
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
		// Don't truncate deltas — they are merged in the frontend
		if delta != "" {
			// Assistant text may introduce another tool call. Suspend fillers
			// during text; only a new tool.start can resume this voice turn.
			// Lifecycle end/error and explicit interruption still hard-cancel.
			sensinghttp.DefaultFillerManager.OnAssistantText(flowRunID)
			h.monitorBus.Push(domain.MonitorEvent{
				Type:    "assistant_delta",
				Summary: delta,
				RunID:   flowRunID,
			})
			if h.recordAssistantDelta(flowRunID, delta) {
				flow.Log("agent_first_token", map[string]any{
					"run_id": flowRunID,
				}, flowRunID)
			}
		}

		// When the agent turn ends, the final assistant text should be spoken.
		// Accumulate deltas per runId and send to TTS when lifecycle "end" arrives.
		h.accumulateAssistantDelta(payload.RunID, delta)

		// Slack (hermes HTTP bridge): stream the reply progressively into the live
		// Slack message (chat.appendStream). Feed the cleaned cumulative text so far;
		// the bridge throttles + appends the new tail. Guarded by the SlackBridge
		// type-assert + IsSlackOriginRun peek — non-Slack runs / openclaw are untouched.
		if sb, ok := h.agentGateway.(domain.SlackBridge); ok && sb.IsSlackOriginRun(flowRunID) {
			if clean, ready := h.cleanedSlackStreamText(payload.RunID); ready {
				sb.StreamSlackDelta(flowRunID, clean)
			}
		}

		// Sentence-streaming: dispatch the FIRST complete sentence to
		// /voice/speak as soon as the agent emits the boundary so the
		// device starts speaking before generation finishes. Only the
		// first sentence streams here — chaining every sentence as its
		// own POST exposes a per-sentence TTFB gap. Lifecycle:end sends
		// the remainder through /voice/speak-queue so Python pre-synths
		// it while sentence 1 plays and chains the rest seamlessly.
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
					// ADDED 2026-05-26: fire leading HW markers SYNC before TTS POST so
					// state mutations (e.g. /scene/off → speaker unmute) apply in HAL
					// before /voice/speak-queue arrives. Without this, TTS races ahead
					// and gets rejected while speaker is still muted by a prior scene.
					// extractLeadingHWCalls only picks markers BEFORE first non-marker
					// text, so inline markers (between sentences) stay deferred to
					// lifecycle:end and preserve position-in-text semantics.
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

	// When agent lifecycle ends, flush accumulated assistant text to TTS.
	// Suppress TTS if the agent played music or already spoke via tool intercept.
	if payload.Stream == "lifecycle" && payload.Data.Phase == "end" {
		lifecycleEndAt := time.Now()
		// Persist streaming summary to JSONL. Raw deltas only live in
		// monitorBus (RAM) — Flow Monitor reads JSONL on reload, so
		// without these summary events the pipeline rect shows no
		// thinking/assistant rows for past turns. Mirror agent_thinking
		// which is similarly populated from chat.history at turn end.
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

		// Hard-cancel any lingering filler before the real TTS flush
		// — covers edge case where the turn ended without any
		// assistant delta (NO_REPLY, HW-only reply, error).
		sensinghttp.DefaultFillerManager.Cancel(flowRunID)
		suppressReason := h.clearTTSSuppress(payload.RunID)
		// Pull interleaved Telegram DM target up front so the map entry is
		// cleared even on NO_REPLY / HW-only / suppressed branches that
		// never reach the dmTelegramID injection below.
		interleavedDMTarget := h.consumeInterleavedDM(payload.RunID)
		if interleavedDMTarget == "" {
			interleavedDMTarget = h.consumeInterleavedDM(flowRunID)
		}
		// Web monitor chat: suppress TTS — response displayed in web UI only.
		if suppressReason == "" && h.agentGateway.ConsumeWebChatRun(flowRunID) {
			suppressReason = "web_chat"
		}
		// Realtime voice agent already spoke this turn (voice_agent_handled):
		// suppress TTS so OpenClaw's reply isn't double-spoken.
		if suppressReason == "" && h.agentGateway.ConsumeSilentRun(flowRunID) {
			suppressReason = "voice_agent_handled"
		}
		text, hwCalls := h.flushAssistantText(payload.RunID)
		finalBufferReadyAt := time.Now()
		// streamedCleanLen > 0 means the first sentence was dispatched
		// mid-turn via tryFirstSentenceFlush; the remainder TTS POST
		// below slices `text` at that offset to skip what already
		// played. Broadcast/DM still use full `text` since it covers
		// the entire reply the user heard.
		streamedLen := h.consumeStreamedCleanLen(payload.RunID)
		if streamedLen > len(text) {
			streamedLen = len(text)
		}
		streamed := streamedLen > 0
		// ADDED 2026-05-26: skip leading HW markers already fired at stream-time
		// (see fireHWCallsSync call earlier). extractHWCalls returns markers in
		// stable source order, so [firedAtStream:] is the remainder.
		firedAtStream := h.consumeFiredHWCount(payload.RunID)
		if firedAtStream > len(hwCalls) {
			firedAtStream = len(hwCalls)
		}
		hwCalls = hwCalls[firedAtStream:]
		if text != "" || len(hwCalls) > 0 || streamed {
			// ADDED 2026-05-27: fire SYNC (was h.fireHWCalls async) so state-
			// changing markers like /scene/off apply before the lifecycle-end
			// TTS POST below races ahead and gets rejected on still-muted
			// speaker. Single-sentence responses skip stream-time fire (no
			// sentence boundary) — without sync here, the race returns.
			// fireHWCallsSync has 100ms per-call timeout + async fallback,
			// so heavy markers (e.g. /servo/track) don't block TTS.
			h.fireHWCallsSync(hwCalls, flowRunID)

			// [2026-05-11] DISABLED — TTS suppress on /audio/play was killing the
			// agent's main reply (e.g. "Mình chọn River Flows in You…") and
			// leaving only the random short backchannel cue. Python music_service
			// already waits for TTS via wait_for_tts() before grabbing ALSA, so
			// this Go-side suppress is redundant. Rollback: uncomment to restore
			// hard-suppress behavior.
			// if suppressReason == "" {
			// 	for _, c := range hwCalls {
			// 		if strings.Contains(c.path, "/audio/play") {
			// 			suppressReason = "music_playing"
			// 			break
			// 		}
			// 	}
			// }

			// Consume broadcast marker early to prevent map leak on NO_REPLY/empty/suppressed paths.
			isBroadcastRun := h.agentGateway.ConsumeBroadcastRun(flowRunID)

			// Slack (hermes HTTP bridge): a Slack-origin turn replies in Slack with TTS
			// suppressed. Peek (don't consume) so the mid-turn streaming gate can also
			// see it; DeliverSlackReply consumes at reply time. isSlackRun is false for
			// every non-Slack run and every runtime that isn't a SlackBridge (openclaw).
			slackBridge, _ := h.agentGateway.(domain.SlackBridge)
			isSlackRun := slackBridge != nil && slackBridge.IsSlackOriginRun(flowRunID)

			// [HW:/broadcast] marker: fan-out reply text to all Telegram chats (guard-only).
			// [HW:/speak] marker: force TTS on the speaker without any channel fan-out —
			// used by proactive triggers (e.g. music suggestions) that run inside a
			// channel session but need to speak out loud anyway.
			// [HW:/dm:{"telegram_id":"123"}] marker: send reply to a specific Telegram user.
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
			// Queue-mode interleave: when the agent didn't include a /dm
			// marker but a Telegram message was injected mid-turn, route
			// the reply back to the originating chat (captured from
			// session.message metadata in the lifecycle window).
			if dmTelegramID == "" && interleavedDMTarget != "" {
				dmTelegramID = interleavedDMTarget
				slog.Info("routing reply to interleaved Telegram chat (queue-mode injection)",
					"component", "agent", "run_id", flowRunID, "chat_id", dmTelegramID)
			}

			// Guard mode: broadcast even on NO_REPLY / empty / suppressed paths.
			// The agent may choose not to speak, but we still want to alert the owner via Telegram.
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
				// Tag the turn so the Flow monitor can label it "heartbeat"
				// instead of rendering an anonymous doppelgänger turn card.
				flow.Log("heartbeat_run", map[string]any{"run_id": flowRunID}, flowRunID)
				h.monitorBus.Push(domain.MonitorEvent{
					Type:    "heartbeat_run",
					Summary: "OpenClaw heartbeat",
					RunID:   flowRunID,
				})
			}
			// Extract <say>...</say> wrapper if the skill uses it (wellbeing).
			// Non-tagged replies pass through unchanged.
			text = extractSayTag(text)
			text = sanitizeAgentText(text)
			// Slice off the prefix already streamed mid-turn so the
			// remainder POST doesn't replay sentence 1. Clamp because
			// extractSayTag / sanitizeAgentText may shorten text below
			// the previously-tracked offset.
			if streamedLen > len(text) {
				streamedLen = len(text)
			}
			remainderText := strings.TrimSpace(text[streamedLen:])
			// CoT-leak filter (see cot_leak_filter.go): drop DeepSeek-style
			// planning monologue from the reply before it reaches TTS, the web
			// chat (full_text), and channel fan-out (DM/broadcast/Slack). The
			// remainder is filtered with state seeded from the already-streamed
			// prefix (CoT-mode + fuzzy-dedup continuity); `text` is replaced by
			// the fresh full-text pass only when something was dropped, so
			// clean turns keep their original whitespace/newlines.
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
				// NO_REPLY in remainder. If streamed > 0 the agent
				// already spoke sentence 1; can't unspeak it. Log a
				// warning so we notice any skill that mixes NO_REPLY
				// with real speech.
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
					// Reply was a single sentence already streamed
					// mid-turn — nothing left to TTS at end. Log so
					// the flow monitor shows turn complete instead
					// of a misleading hw_only_reply.
					slog.Info("assistant turn complete via first-sentence streaming",
						"component", "agent", "run_id", flowRunID, "streamed_len", streamedLen)
					flow.Log("tts_stream_complete", map[string]any{"run_id": flowRunID, "text": text}, flowRunID)
				} else {
					// HW-only reply (only markers, no spoken text)
					flow.Log("hw_only_reply", map[string]any{"run_id": flowRunID}, flowRunID)
				}
			} else if suppressReason != "" {
				h.clearHarnessResponseRun(flowRunID)
				slog.Info("assistant turn done, TTS suppressed", "component", "agent", "reason", suppressReason, "text", text[:min(len(text), 100)])
				flow.Log("tts_suppressed", map[string]any{"run_id": flowRunID, "reason": suppressReason, "text": text}, flowRunID)
			} else {
				h.clearHarnessResponseRun(flowRunID)
				// Channel detection: positive-evidence only. tg- runIDs are
				// synthesised by the device from session.message events (real Telegram
				// users); anything else (device-chat-*, UUID from steer/cron/
				// heartbeat) is NOT a channel run unless explicitly marked
				// via channelRuns below.
				//
				// Previously this defaulted to `!isDeviceOutboundChatRunID(...)`,
				// which mis-classified OpenClaw UUID self-fire / cron / heartbeat
				// runs as Telegram and suppressed their TTS — most visibly,
				// music-suggestion replies on emotion.detected events when the
				// sensing turn got steered into a UUID host turn.
				isChannelRun := isChannelOriginatedRun(payload.RunID, flowRunID)
				// Cron-fire turns always TTS on the device speaker even though their
				// UUID runIds look like channel runs. Detected from chat.history
				// systemEvent template at lifecycle_start (see cronFireRuns map).
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
				// Override: confirmed channel turn via senderLabel always suppresses TTS.
				// Covers race where Telegram UUID mapped to sensing trace (device-sensing-*).
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
					// TTS would be gated by channel_run — log suppression so the
					// monitor doesn't misleadingly show a "tts_send" event when the
					// speaker stays silent. Channel/Telegram users still receive
					// the text via OpenClaw's own session fan-out.
					slog.Info("assistant turn done, TTS suppressed (channel run)", "component", "agent", "text", text[:min(len(text), 100)], "broadcast", isBroadcastRun, "force_tts", forceTTS, "cron_fire", isCronFire, "heartbeat", isHeartbeatRun)
					flow.Log("tts_suppressed", map[string]any{"run_id": flowRunID, "reason": "channel_run", "text": text}, flowRunID)
				} else {
					// remainderText excludes the first sentence already
					// streamed (when streamed=true). Use /voice/speak-queue
					// so Python pre-synthesises while sentence 1 is still
					// playing and chains the remainder seamlessly onto
					// the open ALSA stream (no inter-sentence gap). Non-
					// streamed turns also go through the queue endpoint
					// — when idle it behaves exactly like /voice/speak,
					// so this is a safe drop-in.
					slog.Info("assistant turn done, sending to TTS",
						"component", "agent",
						"text", remainderText[:min(len(remainderText), 100)],
						"streamed_len", streamedLen,
						"broadcast", isBroadcastRun, "force_tts", forceTTS,
						"cron_fire", isCronFire, "heartbeat", isHeartbeatRun)
					// `text` is the FULL reply (sentence 1 + remainder); `remainderText`
					// excludes sentence 1 when it was streamed mid-turn via
					// tts_stream_send. Carry `full_text` so the web (chat + flow turn)
					// can display the complete reply — it only reads tts_send and would
					// otherwise drop sentence 1 (logged separately as tts_stream_send).
					flow.Log("tts_send", map[string]any{"run_id": flowRunID, "text": remainderText, "full_text": text, "streamed_len": streamedLen}, flowRunID)
					slog.Info("[tts-timing] final_dispatch", "run_id", flowRunID,
						"text_key", ttsTextKey(remainderText), "streamed_len", streamedLen,
						"end_to_buffer_ready_ms", finalBufferReadyAt.Sub(lifecycleEndAt).Milliseconds(),
						"buffer_ready_to_dispatch_ms", time.Since(finalBufferReadyAt).Milliseconds())
					h.deliverTTSQueue(remainderText, flowRunID, "TTS delivery failed")
				}
				// Guard broadcast is handled above (before the if/else) to ensure
				// it fires even on NO_REPLY / empty / suppressed paths.
				// DM run: send agent response to a specific Telegram user.
				// Takes priority over broadcast — if /dm is present, /broadcast is skipped.
				if dmTelegramID != "" && len(text) > 10 {
					// Auto-attach the worst pose frames when this turn was
					// triggered by a motion.activity that surfaced a posture
					// nudge. Mirrors the guard-snapshot path: agent doesn't
					// know any file paths — the device resolved them at ingest.
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
					// Broadcast run (e.g. music.mood): send agent response to all channels
					// so user can confirm via Telegram instead of only voice.
					go func(t string) {
						slog.Info("broadcast run response to channels", "component", "agent", "run_id", flowRunID, "text", t[:min(len(t), 80)])
						if err := h.agentGateway.Broadcast(t, ""); err != nil {
							slog.Error("broadcast run failed", "component", "agent", "err", err)
						}
					}(text)
				}
			}

			// Slack (hermes HTTP bridge): finalize the turn for EVERY end-phase outcome
			// (real reply, NO_REPLY, suppressed, empty), not just the spoken-reply branch
			// above — otherwise the per-run stream goroutine + origin/stream maps leak.
			// DeliverSlackReply consumes the origin, stops the stream (chat.stopStream),
			// and posts nothing when the text is empty. NO_REPLY → deliver "" (cleanup).
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
