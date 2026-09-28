package server

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/telemetry"
)

type harnessReply struct {
	answer          bool
	idempotencyKey  string
	overlapped      bool
	localOnly       bool
	restored        bool
	completedResult bool
	agentID         string
	runID           string
	webChat         bool
	created         time.Time
}

type harnessReplyRequest struct {
	RunID   string `json:"run_id"`
	Channel string `json:"channel"`
}

// registerHarnessRoutes exposes management to the owner and commands only to the device runtime.
func (s *Server) registerHarnessRoutes(api *gin.RouterGroup, ctx context.Context) {
	s.initializeHarnessVoice(ctx)
	s.harnessSelector = newHarnessSelector()
	go s.watchHarnessPreparationWaits(ctx)
	group := api.Group("harness")
	group.Use(func(c *gin.Context) {
		if s.harnessService == nil {
			c.AbortWithStatusJSON(http.StatusServiceUnavailable, serializers.ResponseError("Harness service unavailable"))
		}
	})
	group.GET("status", adminOrLoopbackAuth(s.config), func(c *gin.Context) {
		status := s.harnessService.Status()
		// Pairing codes are secrets for the pairing flow; only the authenticated
		// MQTT pairing channel may carry the live code to the mobile owner.
		status.Code = ""
		status.ExpiresAt = 0
		c.JSON(http.StatusOK, serializers.ResponseSuccess(status))
	})
	group.GET("voice-followup", localOnlyMiddleware(), func(c *gin.Context) {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"active": s.HarnessVoiceFollowup()}))
	})
	s.registerHarnessResultRoutes(group)
	s.registerHarnessVoiceRoutes(group)
	// Pairing codes and pinned E2EE identities authenticate the direct socket.
	group.GET("ws", func(c *gin.Context) {
		s.harnessService.ServeHTTP(c.Writer, c.Request)
	})
	group.GET("pair/status", adminAuthMiddleware(s.config), func(c *gin.Context) {
		c.Header("Cache-Control", "no-store")
		c.JSON(http.StatusOK, serializers.ResponseSuccess(s.harnessService.PairStatus()))
	})
	group.POST("pair", adminAuthMiddleware(s.config), func(c *gin.Context) {
		info, err := s.harnessService.StartPair(ctx)
		if err != nil {
			c.JSON(http.StatusConflict, serializers.ResponseError(err.Error()))
			return
		}
		c.Header("Cache-Control", "no-store")
		c.JSON(http.StatusAccepted, serializers.ResponseSuccess(info))
	})
	group.POST("pair/cancel", adminAuthMiddleware(s.config), func(c *gin.Context) {
		if err := s.harnessService.CancelPair(); err != nil {
			c.JSON(http.StatusInternalServerError, serializers.ResponseError("Could not cancel Harness pairing"))
			return
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"cancelled": true}))
	})
	group.DELETE("", adminAuthMiddleware(s.config), func(c *gin.Context) {
		if err := s.harnessService.Unpair(); err != nil {
			c.JSON(http.StatusInternalServerError, serializers.ResponseError("Could not remove Harness pairing"))
			return
		}
		if s.harnessVoice != nil {
			_, _ = s.harnessVoice.SetMode(c.Request.Context(), false)
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"unpaired": true}))
	})
	group.POST("select-agent", localOnlyMiddleware(), func(c *gin.Context) {
		var input struct {
			MachineID string `json:"machineId"`
			AgentID   string `json:"agentId"`
			Text      string `json:"text"`
		}
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 64*1024)
		if err := c.ShouldBindJSON(&input); err != nil || input.MachineID == "" || input.AgentID == "" || strings.TrimSpace(input.Text) == "" || len(input.Text) > 16384 || len(input.AgentID) > 128 || len(input.MachineID) > 128 {
			c.JSON(http.StatusBadRequest, serializers.ResponseError("Invalid Harness selection request"))
			return
		}
		settings := config.JevIntentSettings{}
		if s.config != nil {
			settings = s.config.JevHarnessSettings()
		}
		frame := harness.Frame{"machineId": input.MachineID, "agentId": input.AgentID, "text": input.Text}
		before := s.harnessService.Status()
		selection := s.harnessSelector.selectAgent(c.Request.Context(), frame, before, settings)
		after := s.harnessService.Status()
		if selection.Mode == "jev" && (!after.Connected || before.MachineID != after.MachineID || before.ServerInstanceID != after.ServerInstanceID) {
			selection.Mode, selection.AgentID, selection.Reason = "fallback", input.AgentID, "connection_changed"
		}
		c.JSON(http.StatusOK, serializers.ResponseSuccess(selection))
	})
	group.POST("request", localOnlyMiddleware(), func(c *gin.Context) {
		var frame harness.Frame
		c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 64*1024)
		if err := c.ShouldBindJSON(&frame); err != nil || frame == nil {
			c.JSON(http.StatusBadRequest, serializers.ResponseError("Invalid Harness request"))
			return
		}
		reply, err := extractHarnessReply(frame)
		if err != nil {
			c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
			return
		}
		if reply != nil {
			reply.RunID = canonicalHarnessReplyRunID(reply.RunID)
		}
		kind, _ := frame["type"].(string)
		if err := s.guardHarnessPreparationWait(kind, reply, time.Now()); err != nil {
			c.JSON(http.StatusConflict, serializers.ResponseError(err.Error()))
			return
		}
		agentID, _ := frame["agentId"].(string)
		tracksReply := (kind == "turn.send" || kind == "question.answer") && reply != nil
		// A local Harness agent can complete before Request returns its receipt.
		// Install the local route first so an immediate terminal event is not lost.
		if tracksReply {
			s.registerHarnessDispatch(agentID, reply.RunID, reply.Channel == "web", true, frame)
		}
		requestCtx, cancel := context.WithTimeout(c.Request.Context(), 35*time.Second)
		defer cancel()
		requestStatus := s.harnessService.Status()
		result, err := s.harnessService.Request(requestCtx, frame)
		if kind == "agents.list" {
			currentStatus := s.harnessService.Status()
			if currentStatus.MachineID != requestStatus.MachineID || currentStatus.ServerInstanceID != requestStatus.ServerInstanceID {
				errSnapshot := errors.New("Harness identity changed during discovery")
				s.harnessSelector.remember(nil, errSnapshot, currentStatus)
			} else {
				s.harnessSelector.remember(result, err, currentStatus)
			}
		}
		if err != nil {
			var uncertainDispatch *harness.DeliveryUnknownError
			if tracksReply && !errors.As(err, &uncertainDispatch) {
				s.releaseHarnessPreparationDispatch(reply)
			}
			if tracksReply {
				reportHarnessDispatchError(reply.RunID, "", err)
				if !errors.As(err, &uncertainDispatch) {
					s.forgetHarnessReply(agentID, reply.RunID)
				}
			}
			var preparationUnknown *harness.PreparationUnknownError
			if errors.As(err, &preparationUnknown) {
				c.JSON(http.StatusBadGateway, serializers.ResponseError("Harness preparation is unknown; retry the same preparation key and parameters or poll operation.get; do not use receipt.get or create a new key"))
				return
			}
			var uncertain *harness.DeliveryUnknownError
			if errors.As(err, &uncertain) {
				c.JSON(http.StatusBadGateway, serializers.ResponseError("Harness delivery is unknown; inspect receipt before sending again"))
				return
			}
			c.JSON(http.StatusBadGateway, serializers.ResponseError(err.Error()))
			return
		}
		if tracksReply {
			if result["error"] != nil {
				s.releaseHarnessPreparationDispatch(reply)
			}
			reportHarnessReceipt(reply.RunID, result)
		}
		s.observeHarnessPreparation(kind, reply, result)
		c.JSON(http.StatusOK, serializers.ResponseSuccess(result))
	})
}

// canonicalHarnessReplyRunID repairs the one token a model can occasionally
// stale-copy while constructing the harness.py JSON: the sequence component of
// a device run id. The timestamp is generated by the OS and stays intact. The
// current turn's sensing_input is already in the in-memory flow ring before the
// model can invoke the helper, so it is the authoritative route for that same
// channel and timestamp. Different timestamps are never rewritten.
func canonicalHarnessReplyRunID(runID string) string {
	prefix, stamp, ok := deviceRunPrefixAndStamp(runID)
	if !ok {
		return runID
	}
	for _, event := range flow.Recent(200) {
		candidatePrefix, candidateStamp, candidateOK := deviceRunPrefixAndStamp(event.TraceID)
		if candidateOK && candidatePrefix == prefix && candidateStamp == stamp {
			return event.TraceID
		}
	}
	return runID
}

func deviceRunPrefixAndStamp(runID string) (prefix, stamp string, ok bool) {
	last := strings.LastIndex(runID, "-")
	if last <= 0 || last == len(runID)-1 {
		return "", "", false
	}
	stamp = runID[last+1:]
	for _, ch := range stamp {
		if ch < '0' || ch > '9' {
			return "", "", false
		}
	}
	base := runID[:last]
	sequence := strings.LastIndex(base, "-")
	if sequence <= 0 || !strings.HasPrefix(base[:sequence], "device-") {
		return "", "", false
	}
	for _, ch := range base[sequence+1:] {
		if ch < '0' || ch > '9' {
			return "", "", false
		}
	}
	return base[:sequence], stamp, true
}

func extractHarnessReply(frame harness.Frame) (*harnessReplyRequest, error) {
	raw, present := frame["response"]
	delete(frame, "response") // local routing metadata is never sent to Harness.
	if !present {
		return nil, nil
	}
	encoded, ok := raw.(map[string]any)
	if !ok {
		return nil, errors.New("Invalid Harness response routing")
	}
	reply := &harnessReplyRequest{}
	if value, ok := encoded["run_id"].(string); ok {
		reply.RunID = strings.TrimSpace(value)
	}
	if value, ok := encoded["channel"].(string); ok {
		reply.Channel = strings.TrimSpace(value)
	}
	if reply.RunID == "" || len(reply.RunID) > 128 || (reply.Channel != "voice" && reply.Channel != "web") {
		return nil, errors.New("Invalid Harness response routing")
	}
	return reply, nil
}

func (s *Server) registerHarnessReply(agentID, runID string, webChat, delegated bool) {
	s.registerHarnessRoute(agentID, runID, webChat, delegated, "", false)
}

func (s *Server) registerHarnessRoute(agentID, runID string, webChat, delegated bool, key string, localOnly bool) {
	s.registerHarnessRouteWithAnswer(agentID, runID, webChat, delegated, key, localOnly, false)
}

func (s *Server) registerHarnessRouteWithAnswer(agentID, runID string, webChat, delegated bool, key string, localOnly, answer bool) {
	if agentID == "" || runID == "" || s.agentHandler == nil {
		return
	}
	s.harnessRepliesMu.Lock()
	if s.harnessReplies == nil {
		s.harnessReplies = make(map[string]harnessReply)
	}
	if s.harnessOverlapAgents == nil {
		s.harnessOverlapAgents = make(map[string]bool)
	}
	route := harnessReply{agentID: agentID, runID: runID, webChat: webChat, created: time.Now(), overlapped: s.harnessOverlapAgents[agentID], idempotencyKey: key, localOnly: localOnly, answer: answer}
	for id, other := range s.harnessReplies {
		if !answer && !other.answer && !localOnly && !other.localOnly && !other.completedResult && id != runID && other.agentID == agentID && time.Since(other.created) <= 15*time.Minute {
			other.overlapped, route.overlapped = true, true
			s.harnessOverlapAgents[agentID] = true
			s.harnessReplies[id] = other
		}
	}
	s.harnessReplies[runID] = route
	s.harnessRepliesMu.Unlock()
	s.harnessFollowup.Store(time.Now().Add(2 * time.Minute).UnixMilli())
	if localOnly {
		s.agentHandler.MarkHarnessLocalResponseRun(runID, webChat)
	} else {
		s.agentHandler.MarkHarnessResponseRun(runID, webChat, delegated)
	}
	telemetry.ReportTaskExecution(runID, "", "unknown", "harness_delegated")
}

func (s *Server) forgetHarnessReply(agentID, runID string) {
	s.takeHarnessReply(agentID, runID)
}

// takeHarnessReply removes one exact route and reports whether this caller won
// the route. Replayed summaries may race; only one may publish the final
// result for the original device turn.
func (s *Server) takeHarnessReply(agentID, runID string) bool {
	s.harnessRepliesMu.Lock()
	defer s.harnessRepliesMu.Unlock()
	if current, ok := s.harnessReplies[runID]; ok && current.agentID == agentID {
		delete(s.harnessReplies, runID)
		return true
	}
	return false
}

// HarnessVoiceFollowup keeps short spoken clarifications with the paired agent.
func (s *Server) HarnessVoiceFollowup() bool {
	return time.Now().UnixMilli() < s.harnessFollowup.Load()
}

// HarnessFollowupContext returns the latest direct Harness result while its
// follow-up window is open. It is injected as untrusted context into the next
// user turn so the device agent can answer a clarification without pretending
// it generated or observed the remote result itself.
func (s *Server) HarnessFollowupContext() string {
	if !s.HarnessVoiceFollowup() {
		return ""
	}
	s.harnessResultMu.RLock()
	defer s.harnessResultMu.RUnlock()
	if time.Since(s.harnessResultAt) > 15*time.Minute {
		return ""
	}
	return s.harnessResult
}

func (s *Server) rememberHarnessResult(agentID, runID, text string) {
	text = strings.TrimSpace(text)
	if text == "" {
		return
	}
	s.harnessResultMu.Lock()
	// Preserve transport-owned provenance alongside untrusted result text. The
	// next turn must not combine one agent's result with another saved target.
	payload, _ := json.Marshal(struct {
		AgentID       string `json:"agentId"`
		ResponseRunID string `json:"responseRunId"`
		Text          string `json:"text"`
	}{agentID, runID, text})
	s.harnessResult = string(payload)
	s.harnessResultAt = time.Now()
	s.harnessResultMu.Unlock()
	s.harnessFollowup.Store(time.Now().Add(2 * time.Minute).UnixMilli())
}

// forwardHarnessEvent relays real Harness lifecycle events to the original
// device interaction. The terminal recap is not passed through the device
// agent, which would otherwise add a slower, altered second answer.
func (s *Server) forwardHarnessEvent(frame harness.Frame) {
	if hasHarnessSummaryResult(frame) {
		// The same summary event is already in the durable result inbox.
		// Never route its group through the one-input compatibility path.
		return
	}
	if frame["kind"] == "receipt.updated" {
		if provenance, ok := frame["_osResultContext"].(harness.ResultContext); ok {
			if payload, ok := frame["payload"].(map[string]any); ok {
				s.bindHarnessResultReceipt(harness.Frame(payload), provenance)
			}
		}
	}
	if kind, _ := frame["kind"].(string); kind == "focus.changed" {
		if s.harnessVoice != nil {
			s.harnessVoice.NotifyFocusChanged()
		}
		return
	}
	agentID, _ := frame["agentId"].(string)
	if agentID == "" {
		// Some Harness transports identify the originating request by run ID
		// instead of agent ID. Resolve that directly to the pending device turn.
		runID, _ := frame["runId"].(string)
		if runID == "" {
			runID, _ = frame["run_id"].(string)
		}
		if payload, ok := frame["payload"].(map[string]any); ok && runID == "" {
			runID, _ = payload["runId"].(string)
			if runID == "" {
				runID, _ = payload["run_id"].(string)
			}
		}
		if runID != "" {
			s.harnessRepliesMu.Lock()
			if reply, ok := s.harnessReplies[runID]; ok {
				agentID = reply.agentID
			}
			s.harnessRepliesMu.Unlock()
		}
	}
	if agentID == "" {
		agentID, _ = frame["agent_id"].(string)
	}
	if agentID == "" {
		if payload, ok := frame["payload"].(map[string]any); ok {
			agentID, _ = payload["agentId"].(string)
			if agentID == "" {
				agentID, _ = payload["agent_id"].(string)
			}
		}
	}
	if agentID == "" {
		return
	}

	kind, _ := frame["kind"].(string)
	s.observeHarnessExecution(agentID, kind, frame)
	if kind == "turn.tool" {
		toolName, toolArgs := harnessToolEvent(frame)
		if toolName == "" {
			return
		}
		s.harnessRepliesMu.Lock()
		reply, ok := s.harnessReplyForFrameLocked(agentID, frame)
		s.harnessRepliesMu.Unlock()
		if ok && time.Since(reply.created) <= 15*time.Minute {
			s.agentHandler.DeliverHarnessTool(reply.runID, toolName, toolArgs)
			s.agentHandler.AnnounceHarnessProgress(reply.runID, harnessToolProgressText(toolName, toolArgs))
		}
		return
	}
	text := harnessEventText(kind, frame)
	if provenance, ok := frame["_osResultContext"].(harness.ResultContext); ok && s.acceptsHarnessResults(provenance) {
		if progress := harnessInputProgress(frame); progress != "" {
			text = progress
		}
	}
	if text == "" && kind != "turn.summary" {
		return
	}
	s.harnessRepliesMu.Lock()
	reply, ok := s.harnessReplyForFrameLocked(agentID, frame)
	terminal := kind == "turn.summary" || kind == "turn.error" || kind == "agent.error" || kind == "question.open"
	s.harnessRepliesMu.Unlock()
	if !ok || time.Since(reply.created) > 15*time.Minute {
		return
	}
	if kind == "question.open" {
		payload, _ := frame["payload"].(map[string]any)
		questionID, _ := payload["questionRequestId"].(string)
		s.agentHandler.DeliverHarnessQuestion(reply.runID, questionID, text)
		return
	}
	if !terminal {
		s.agentHandler.DeliverHarnessProgress(reply.runID, text)
		// turn.done lands milliseconds before its result; speaking "finished,
		// receiving its result" just delays the result itself.
		if kind != "turn.done" {
			s.agentHandler.AnnounceHarnessProgress(reply.runID, text)
		}
		return
	}
	// A legacy single-input summary uses only the event's own text. Neither
	// turn.done nor latest recap can establish a result's original membership.

	if strings.TrimSpace(text) == "" {
		slog.Warn("Harness summary has no result yet", "component", "harness", "run_id", reply.runID, "agent_id", agentID)
		return
	}
	// Consume only the exact original response route.
	s.deliverHarnessFinal(agentID, reply.runID, text)
}

// harnessToolProgressText describes a remote tool start for the announcer,
// which turns it into one spoken sentence at most.
func harnessToolProgressText(toolName, toolArgs string) string {
	text := "Harness agent is using the tool " + toolName + "."
	if args := []rune(strings.TrimSpace(toolArgs)); len(args) > 0 {
		if len(args) > 300 {
			args = args[:300]
		}
		text += " Arguments: " + string(args)
	}
	return text
}

func (s *Server) hasHarnessReply(agentID, runID string) bool {
	s.harnessRepliesMu.Lock()
	defer s.harnessRepliesMu.Unlock()
	reply, ok := s.harnessReplies[runID]
	return ok && reply.agentID == agentID
}

func (s *Server) deliverHarnessFinal(agentID, runID, text string) {
	text = strings.TrimSpace(text)
	if text == "" {
		return
	}
	if !s.hasHarnessReply(agentID, runID) {
		return
	}
	if err := s.completeHarnessHistory(runID, text); err != nil {
		slog.Warn("persist Harness conversation result failed; retaining reply route", "run_id", runID, "error", err)
		return
	}
	if !s.takeHarnessReply(agentID, runID) {
		return
	}
	s.rememberHarnessResult(agentID, runID, text)
	if !s.agentHandler.DeliverHarnessResponse(runID, text) {
		slog.Warn("Harness result had no pending device turn", "component", "harness", "run_id", runID)
	}
}

// registerHarnessDispatch installs correlation before the transport can emit events.
func (s *Server) registerHarnessDispatch(agentID, runID string, webChat, delegated bool, frame harness.Frame) {
	key, _ := frame["idempotencyKey"].(string)
	s.registerHarnessRouteWithAnswer(agentID, runID, webChat, delegated, key, false, frame["type"] == "question.answer")
}

// Explicit but unknown IDs never fall back. Legacy agent-only events are safe
// only for a single route which has never overlapped another outstanding turn.
func (s *Server) harnessReplyForEventLocked(agentID, eventRunID string) (harnessReply, bool) {
	return s.harnessReplyForFrameLocked(agentID, harness.Frame{"runId": eventRunID})
}
func (s *Server) harnessReplyForFrameLocked(agentID string, frame harness.Frame) (harnessReply, bool) {
	runID := harnessFrameRunID(frame)
	key, _ := frame["idempotencyKey"].(string)
	if payload, ok := frame["payload"].(map[string]any); ok && key == "" {
		key, _ = payload["idempotencyKey"].(string)
		if receipt, ok := payload["receipt"].(map[string]any); ok && key == "" {
			key, _ = receipt["idempotencyKey"].(string)
		}
	}
	var selected harnessReply
	count := 0
	for _, reply := range s.harnessReplies {
		if reply.answer || reply.localOnly || reply.completedResult || reply.agentID != agentID || time.Since(reply.created) > 15*time.Minute {
			continue
		}
		if runID != "" && reply.runID != runID {
			continue
		}
		if key != "" && reply.idempotencyKey != key {
			continue
		}
		if runID == "" && key == "" && reply.overlapped {
			continue
		}
		selected = reply
		count++
	}
	return selected, count == 1
}

func harnessFrameRunID(frame harness.Frame) string {
	for _, key := range []string{"runId", "run_id"} {
		if value, _ := frame[key].(string); value != "" {
			return value
		}
	}
	if payload, ok := frame["payload"].(map[string]any); ok {
		for _, key := range []string{"runId", "run_id"} {
			if value, _ := payload[key].(string); value != "" {
				return value
			}
		}
	}
	return ""
}

func harnessToolEvent(frame harness.Frame) (name, args string) {
	payload, _ := frame["payload"].(map[string]any)
	if payload == nil {
		return "", ""
	}
	name, _ = payload["text"].(string)
	if detail, _ := payload["detail"].(string); detail != "" {
		args = detail
	} else {
		args, _ = payload["recap"].(string)
	}
	return strings.TrimSpace(name), strings.TrimSpace(args)
}

// harnessRecapText reads the complete final text stored by Harness. Summary
// events deliberately carry a compact preview for device tiles, so that
// preview is used only if the read-only recap request cannot be completed.
func (s *Server) harnessRecapText(agentID, fallback string) string {
	if s.harnessService == nil || agentID == "" {
		return fallback
	}
	machineID := s.harnessService.Status().MachineID
	if machineID == "" {
		return fallback
	}
	parent := s.harnessVoiceCtx
	if parent == nil {
		parent = context.Background()
	}
	ctx, cancel := context.WithTimeout(parent, 5*time.Second)
	defer cancel()
	frame, err := s.harnessService.Request(ctx, harness.Frame{
		"type":      "recap",
		"machineId": machineID,
		"agentId":   agentID,
		"n":         1,
	})
	if err != nil {
		slog.Debug("Harness recap unavailable; using summary preview", "component", "harness", "agent_id", agentID, "error", err)
		return fallback
	}
	if text := harnessRecapResultText(frame); text != "" {
		return text
	}
	return fallback
}

// harnessRecapResultText extracts the latest complete user-facing message
// from the read-only recap result. fullText is supplied by current Harness
// CLIs for readers such as voice; text is the compact legacy preview.
func harnessRecapResultText(frame harness.Frame) string {
	turns, _ := frame["turns"].([]any)
	for _, raw := range turns {
		turn, _ := raw.(map[string]any)
		if fullText, _ := turn["fullText"].(string); strings.TrimSpace(fullText) != "" {
			return strings.TrimSpace(fullText)
		}
		if text, _ := turn["text"].(string); strings.TrimSpace(text) != "" {
			return strings.TrimSpace(text)
		}
	}
	return ""
}

func harnessEventText(kind string, frame harness.Frame) string {
	payload, _ := frame["payload"].(map[string]any)
	if payload == nil {
		return ""
	}
	if kind == "receipt.updated" {
		receipt, _ := payload["receipt"].(map[string]any)
		state, _ := receipt["state"].(string)
		switch state {
		case "queued":
			return "Harness queued the request."
		case "delivered":
			return "Harness received the request."
		case "started":
			return "Harness agent is working."
		}
		return ""
	}
	if kind == "turn.started" {
		return "Harness agent is working."
	}
	if kind == "turn.done" {
		return "Harness agent finished; receiving its result."
	}
	if kind == "question.open" {
		questions, _ := payload["questions"].([]any)
		for _, raw := range questions {
			question, _ := raw.(map[string]any)
			for _, key := range []string{"q", "question", "prompt", "text"} {
				if text, _ := question[key].(string); strings.TrimSpace(text) != "" {
					text = strings.TrimSpace(text)
					options, _ := question["options"].([]any)
					for _, rawOption := range options {
						if option, ok := rawOption.(string); ok && strings.TrimSpace(option) != "" {
							text += "\n" + option
						}
					}
					return text
				}
			}
		}
		return "Harness needs an answer before it can continue."
	}
	if kind == "turn.error" {
		return "Harness stopped before it returned a result."
	}
	if kind == "turn.summary" {
		if fullText, _ := payload["fullText"].(string); strings.TrimSpace(fullText) != "" {
			return strings.TrimSpace(fullText)
		}
	}
	text, _ := payload["text"].(string)
	if kind == "turn.summary" && strings.TrimSpace(text) == "" {
		text, _ = payload["recap"].(string)
	}
	if kind != "turn.summary" && kind != "turn.error" && kind != "agent.error" {
		return ""
	}
	return strings.TrimSpace(text)
}
