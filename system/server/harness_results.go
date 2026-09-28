package server

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"path/filepath"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/harness"
	agenthttp "go.autonomous.ai/os/system/server/agent/delivery/http"
	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/telemetry"
)

// Result correlation is part of the existing summary flow, not an optional mode.
func (s *Server) initializeHarnessResults(path string) error {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return err
	}
	store, err := harness.OpenResultStore(absolute)
	if err != nil {
		return err
	}
	s.harnessResults = store
	s.harnessResultWake = make(chan struct{}, 1)
	return nil
}

func (s *Server) acceptsHarnessResults(peer harness.ResultContext) bool {
	return s.harnessResults != nil && peer.Owner != "" && peer.ServerInstanceID != ""
}

func (s *Server) wakeHarnessResults() {
	select {
	case s.harnessResultWake <- struct{}{}:
	default:
	}
}

func (s *Server) reserveHarnessResult(frame harness.Frame, peer harness.ResultContext) error {
	kind, _ := frame["type"].(string)
	if kind != "turn.send" && kind != "question.answer" {
		return nil
	}
	key, _ := frame["idempotencyKey"].(string)
	agentID, _ := frame["agentId"].(string)
	s.harnessRepliesMu.Lock()
	var route harnessReply
	found := false
	for _, candidate := range s.harnessReplies {
		if candidate.agentID == agentID && candidate.idempotencyKey == key && !candidate.localOnly && !candidate.completedResult {
			if found {
				s.harnessRepliesMu.Unlock()
				return errors.New("ambiguous Harness response route")
			}
			route, found = candidate, true
		}
	}
	s.harnessRepliesMu.Unlock()
	if !found {
		return nil // No local response address was requested for this command.
	}
	if !s.acceptsHarnessResults(peer) {
		return errors.New("Harness result storage unavailable; input was not sent")
	}
	channel, destination := "voice", "voice:main:0"
	if s.harnessVoice != nil {
		destination = fmt.Sprintf("voice:main:%d", s.harnessVoice.State().Generation)
	}
	if route.webChat {
		channel, destination = "web", "web:"+route.runID
	} else if strings.HasPrefix(route.runID, "device-harness-") {
		if s.harnessVoice == nil {
			return errors.New("Harness voice destination unavailable")
		}
		state := s.harnessVoice.State()
		if !state.Enabled || state.AgentID != agentID || state.MachineID != peer.MachineID {
			return errors.New("Harness voice destination changed")
		}
		destination = fmt.Sprintf("voice:harness:%d:%s", state.Generation, state.FocusRevision)
	}
	text, _ := frame["text"].(string)
	if kind == "question.answer" {
		b, err := json.Marshal(frame["answers"])
		if err != nil {
			return err
		}
		text = string(b)
	}
	in := harness.ResultInput{Owner: peer.Owner, ServerInstanceID: peer.ServerInstanceID, AgentID: agentID, IdempotencyKey: key,
		RunID: route.runID, Text: text, Channel: channel, Destination: destination, ExpiresAt: route.created.Add(15 * time.Minute)}
	reservations := s.harnessResults.Inputs()
	for _, answer := range s.harnessResults.Answers() {
		reservations = append(reservations, answer.Input)
	}
	for _, old := range reservations {
		if old.Owner == in.Owner && old.IdempotencyKey == key && old.ServerInstanceID != in.ServerInstanceID {
			return errors.New("Harness key belongs to an earlier server instance; reconcile its original receipt without resending")
		}
		if old.Owner == in.Owner && old.ServerInstanceID == in.ServerInstanceID && old.IdempotencyKey == key {
			// Retries retain the original deadline, not a fresh fifteen-minute lease.
			in.ExpiresAt = old.ExpiresAt
		}
	}
	if kind == "question.answer" {
		questionID, _ := frame["questionRequestId"].(string)
		_, err := s.harnessResults.ReserveAnswer(in, questionID)
		if errors.Is(err, harness.ErrResultQuestionUnbound) {
			err = s.harnessResults.ReserveUnlinkedAnswer(in, questionID)
		}
		if err != nil {
			return err
		}
	} else if err := s.harnessResults.Reserve(in); err != nil {
		return err
	}
	s.harnessRepliesMu.Lock()
	current, ok := s.harnessReplies[route.runID]
	matched := ok && current.agentID == agentID && current.idempotencyKey == key
	s.harnessRepliesMu.Unlock()
	if !matched {
		return errors.New("Harness result route changed before dispatch")
	}
	return nil
}

func (s *Server) bindHarnessResultReceipt(frame harness.Frame, peer harness.ResultContext) {
	if !s.acceptsHarnessResults(peer) {
		return
	}
	receipt, ok := frame["receipt"].(map[string]any)
	if !ok {
		return
	}
	value := func(key string) string { v, _ := receipt[key].(string); return v }
	if value("serverInstanceId") == "" || (value("machineId") != "" && value("machineId") != peer.MachineID) {
		return
	}
	for _, answer := range s.harnessResults.Answers() {
		in := answer.Input
		if in.Owner != peer.Owner || in.ServerInstanceID != value("serverInstanceId") || in.IdempotencyKey != value("idempotencyKey") {
			continue
		}
		if _, err := s.harnessResults.BindAnswerReceipt(peer.Owner, in.ServerInstanceID, value("agentId"), in.IdempotencyKey, value("deliveryId"), value("state")); err != nil {
			slog.Warn("Harness answer receipt rejected", "error", err)
			return
		}
		s.wakeHarnessResults()
		return
	}
	// Only receipts matching a local reservation can introduce delivery IDs.
	for _, in := range s.harnessResults.Inputs() {
		if in.Owner != peer.Owner || in.ServerInstanceID != value("serverInstanceId") || in.IdempotencyKey != value("idempotencyKey") {
			continue
		}
		if err := s.harnessResults.BindReceipt(peer.Owner, in.ServerInstanceID, value("agentId"), in.IdempotencyKey, value("deliveryId")); err != nil {
			slog.Warn("Harness result receipt rejected", "error", err)
			return
		}
		s.wakeHarnessResults()
		return
	}
}

func (s *Server) captureHarnessResult(frame harness.Frame, peer harness.ResultContext) error {
	if frame["type"] == "event" && frame["kind"] == "question.open" {
		return s.captureHarnessQuestion(frame, peer)
	}
	if !hasHarnessSummaryResult(frame) {
		return nil
	}
	if !s.acceptsHarnessResults(peer) {
		return errors.New("Harness summary result cannot be durably stored")
	}
	_, _, err := harness.ParseSummaryResult(frame)
	if err != nil {
		return err
	}
	// Envelope identity belongs to the live transport; payload identity belongs
	// to the original receipt and may predate a daemon restart.
	if transport, ok := frame["serverInstanceId"].(string); ok && transport != peer.ServerInstanceID {
		return errors.New("Harness summary transport instance mismatch")
	}
	if machine, ok := frame["machineId"].(string); ok && machine != peer.MachineID {
		return errors.New("Harness summary transport machine mismatch")
	}
	// Stage the wire frame before advancing replay, not the locally injected metadata.
	clean := harness.Frame{}
	for key, value := range frame {
		if key != "_osResultContext" {
			clean[key] = value
		}
	}
	if err := s.harnessResults.Stage(peer.Owner, clean); err != nil {
		return err
	}
	s.wakeHarnessResults()
	return nil
}

func (s *Server) watchHarnessResults(ctx context.Context) {
	if s.harnessResults == nil {
		return
	}
	timer := time.NewTicker(30 * time.Second)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
		case <-s.harnessResultWake:
		}
		if s.harnessService == nil {
			continue
		}
		peer := s.harnessService.ResultContext()
		if !s.acceptsHarnessResults(peer) {
			continue
		}
		s.reconcileHarnessResultReceipts(ctx, peer)
		s.processHarnessResults(peer)
	}
}

// Reconciliation is read-only and bounded. Never resend a turn or generate a key.
func (s *Server) reconcileHarnessResultReceipts(ctx context.Context, peer harness.ResultContext) {
	keys := map[string]bool{}
	for _, staged := range s.harnessResults.Staged() {
		if staged.Owner != peer.Owner {
			continue
		}
		_, p, err := harness.ParseSummaryResult(staged.Frame)
		if err != nil {
			continue
		}
		for _, m := range p.Correlation.Inputs {
			keys[p.ServerInstanceID+"\x00"+m.IdempotencyKey] = true
		}
	}
	candidates := []harness.ResultInput{}
	for _, in := range s.harnessResults.Inputs() {
		if keys[in.ServerInstanceID+"\x00"+in.IdempotencyKey] && in.DeliveryID == "" {
			candidates = append(candidates, in)
		}
	}
	for _, answer := range s.harnessResults.Answers() {
		if answer.ReceiptState != "completed" && answer.ReceiptState != "rejected" {
			candidates = append(candidates, answer.Input)
		}
	}
	attempts := 0
	for _, in := range candidates {
		if in.Owner != peer.Owner {
			continue
		}
		if attempts >= 8 {
			break
		}
		attempts++
		if current := s.harnessService.ResultContext(); current != peer {
			return
		}
		requestCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		_, _ = s.harnessService.Request(requestCtx, harness.Frame{"type": "receipt.get", "idempotencyKey": in.IdempotencyKey})
		cancel()
	}
}

func (s *Server) processHarnessResults(peer harness.ResultContext) {
	if !s.acceptsHarnessResults(peer) {
		return
	}
	if s.harnessService != nil && s.harnessService.ResultContext() != peer {
		return
	}
	s.harnessResultsMu.Lock()
	defer s.harnessResultsMu.Unlock()
	for _, staged := range s.harnessResults.Staged() {
		if staged.Owner != peer.Owner {
			continue
		}
		_, p, err := harness.ParseSummaryResult(staged.Frame)
		if err != nil {
			continue
		}
		if record, fresh, err := s.harnessResults.Apply(peer.Owner, staged.Frame, time.Now()); err != nil {
			// A missing or conflicting member blocks the whole group. Keep the inbox.
			slog.Warn("Harness result awaits receipt reconciliation", "result_id", p.ResultID, "error", err)
		} else if fresh {
			for _, in := range record.Inputs {
				telemetry.ReportTaskExecution(in.RunID, "", record.Payload.Outcome, "harness_correlated_summary")
			}
		}
	}
	s.deliverHarnessAnswerReceipts(peer)
	for _, result := range s.harnessResults.Results() {
		if result.Owner != peer.Owner {
			continue
		}
		s.deliverStoredHarnessResult(peer, result)
	}
}

func (s *Server) harnessResultDestinationCurrent(peer harness.ResultContext, r harness.ResultRecord) bool {
	if s.harnessService == nil || s.harnessService.ResultContext() != peer {
		return false
	}
	if strings.HasPrefix(r.Destination, "voice:main:") {
		if s.harnessVoice == nil {
			return r.Destination == "voice:main:0"
		}
		state := s.harnessVoice.State()
		return !state.Enabled && r.Destination == fmt.Sprintf("voice:main:%d", state.Generation)
	}
	if !strings.HasPrefix(r.Destination, "voice:harness:") || s.harnessVoice == nil {
		return false
	}
	state := s.harnessVoice.State()
	return state.Enabled && state.AgentID == r.AgentID && state.MachineID == peer.MachineID &&
		r.Destination == fmt.Sprintf("voice:harness:%d:%s", state.Generation, state.FocusRevision)
}

func harnessResultReference(r harness.ResultRecord) string {
	// Opaque local retrieval ID; remote identifiers cannot inject URLs or markup.
	b, _ := json.Marshal([]string{r.Owner, r.Payload.ServerInstanceID, r.Payload.ResultID})
	digest := sha256.Sum256(b)
	return hex.EncodeToString(digest[:])
}

func (s *Server) deliverStoredHarnessResult(peer harness.ResultContext, r harness.ResultRecord) {
	ref := harnessResultReference(r)
	if s.harnessResultsPublished[ref] {
		return
	}
	// A durable result can restore its own display address after OS restart,
	// without reviving unrelated historical tasks or authorizing old speech.
	for _, in := range r.Inputs {
		in.ResultID = r.Payload.ResultID
		s.restoreHarnessResultRoute(in)
	}
	runIDs := make([]string, 0, len(r.Inputs))
	live := true
	speakerCurrent := true
	s.harnessRepliesMu.Lock()
	for _, in := range r.Inputs {
		runIDs = append(runIDs, in.RunID)
		route, ok := s.harnessReplies[in.RunID]
		if route.restored {
			speakerCurrent = false
		}
		if !ok || route.agentID != in.AgentID || route.idempotencyKey != in.IdempotencyKey {
			live = false
		}
	}
	s.harnessRepliesMu.Unlock()
	// The result record is the shared source of truth. History entries contain
	// references, never separate claims that the merged text belongs to each input.
	for _, in := range r.Inputs {
		if err := s.completeHarnessHistory(in.RunID, fmt.Sprintf("Harness shared result %s (%s). Retrieve /api/harness/results/%s; this reference does not imply audible playback.", ref, r.Payload.Outcome, ref)); err != nil {
			slog.Warn("Harness result history reference pending", "result_id", r.Payload.ResultID, "error", err)
			return
		}
	}
	// Persist the outbox claim before any irreversible notification. A failed
	// claim leaves UI and speech eligibility intact for a later storage retry.
	claimed := false
	if r.SpeechState == "pending" {
		var err error
		_, claimed, err = s.harnessResults.Claim(r.Owner, r.Payload.ServerInstanceID, r.Payload.ResultID, time.Now())
		if err != nil {
			slog.Warn("Harness result outbox claim failed", "error", err)
			return
		}
	}
	s.harnessRepliesMu.Lock()
	for _, in := range r.Inputs {
		if route, ok := s.harnessReplies[in.RunID]; ok && route.agentID == in.AgentID && route.idempotencyKey == in.IdempotencyKey {
			route.completedResult = true
			s.harnessReplies[in.RunID] = route
		}
	}
	s.harnessRepliesMu.Unlock()
	if live && s.agentHandler != nil {
		live = s.agentHandler.DeliverHarnessGroupedResult(ref, r.Payload.Outcome, r.Payload.FullText, runIDs)
		if live {
			s.rememberHarnessGroupedResult(r, runIDs)
		}
	}
	if claimed {
		state := "suppressed"
		if live && speakerCurrent && s.agentHandler != nil && s.harnessResultDestinationCurrent(peer, r) {
			err := s.agentHandler.SpeakHarnessGroupedResult(r.Payload.FullText, r.Payload.Outcome, runIDs)
			state = "accepted"
			if errors.Is(err, agenthttp.ErrHarnessResultSpeechSuppressed) {
				state = "suppressed"
			} else if err != nil {
				state = "uncertain"
			}
		}
		if err := s.harnessResults.Finish(r.Owner, r.Payload.ServerInstanceID, r.Payload.ResultID, state); err != nil {
			// A claimed record becomes uncertain on restart. Never enqueue it again.
			slog.Warn("Harness result speech acknowledgement not persisted", "error", err)
		}
	}
	if s.harnessResultsPublished == nil {
		s.harnessResultsPublished = make(map[string]bool)
	}
	s.harnessResultsPublished[ref] = true
	for _, in := range r.Inputs {
		s.harnessRepliesMu.Lock()
		route, ok := s.harnessReplies[in.RunID]
		if ok && route.agentID == in.AgentID && route.idempotencyKey == in.IdempotencyKey {
			delete(s.harnessReplies, in.RunID)
		}
		s.harnessRepliesMu.Unlock()
	}
}

func (s *Server) registerHarnessResultRoutes(group *gin.RouterGroup) {
	group.GET("results/:id", localOnlyMiddleware(), func(c *gin.Context) {
		c.Header("Cache-Control", "no-store")
		if s.harnessResults == nil || s.harnessService == nil {
			c.JSON(http.StatusNotFound, serializers.ResponseError("Harness result unavailable"))
			return
		}
		owner := s.harnessService.ResultOwner()
		for _, r := range s.harnessResults.Results() {
			if owner != "" && r.Owner == owner && harnessResultReference(r) == c.Param("id") {
				c.JSON(http.StatusOK, serializers.ResponseSuccess(r))
				return
			}
		}
		c.JSON(http.StatusNotFound, serializers.ResponseError("Harness result unavailable"))
	})
}

// receipt.input describes admission into an engine, never result membership.
func harnessInputProgress(frame harness.Frame) string {
	if frame["kind"] != "receipt.updated" {
		return ""
	}
	payload, _ := frame["payload"].(map[string]any)
	receipt, _ := payload["receipt"].(map[string]any)
	input, _ := receipt["input"].(map[string]any)
	phase, _ := input["phase"].(string)
	mode, _ := input["mode"].(string)
	switch phase {
	case "waiting_for_writer":
		return "Waiting for the agent input channel."
	case "waiting_for_user":
		return "The agent needs user action before accepting this input."
	case "waiting_for_turn":
		return "Input queued; waiting for the current turn."
	case "submitted":
		return "Input submitted; acceptance is not confirmed yet."
	case "unconfirmed":
		return "Input acceptance is unconfirmed; checking its original receipt."
	case "accepted":
		switch mode {
		case "steering":
			return "The agent accepted the follow-up as steering; waiting for its result."
		case "native_queue", "daemon_queue":
			return "Input accepted into the queue; execution is not confirmed yet."
		case "direct", "native_input":
			return "The agent accepted the input; waiting for its result."
		}
	}
	return ""
}

// A shared answer retains group provenance; it is not attributed to one input.
func (s *Server) rememberHarnessGroupedResult(r harness.ResultRecord, runIDs []string) {
	b, _ := json.Marshal(struct {
		AgentID        string   `json:"agentId"`
		ResultID       string   `json:"resultId"`
		ResponseRunIDs []string `json:"responseRunIds"`
		Outcome        string   `json:"outcome"`
		Text           string   `json:"text"`
	}{r.AgentID, harnessResultReference(r), runIDs, r.Payload.Outcome, r.Payload.FullText})
	s.harnessResultMu.Lock()
	s.harnessResult = string(b)
	s.harnessResultAt = time.Now()
	s.harnessResultMu.Unlock()
	s.harnessFollowup.Store(time.Now().Add(2 * time.Minute).UnixMilli())
}

// Presence of any new result field commits a summary to strict correlation.
// Malformed metadata must never fall through to legacy agent/latest-run routing.
func hasHarnessSummaryResult(frame harness.Frame) bool {
	if frame["kind"] != "turn.summary" {
		return false
	}
	payload := harnessSummaryPayload(frame)
	for _, field := range []string{"resultId", "serverInstanceId", "correlation", "outcome"} {
		if _, present := payload[field]; present {
			return true
		}
	}
	return false
}

func harnessSummaryPayload(frame harness.Frame) map[string]any {
	switch payload := frame["payload"].(type) {
	case map[string]any:
		return payload
	case harness.Frame:
		return map[string]any(payload)
	default:
		return nil
	}
}

// Restore only an address needed by a proven result or a known waiting history
// entry. This is not a new dispatch: no overlap, follow-up or execution metric.
func (s *Server) restoreHarnessResultRoute(in harness.ResultInput) {
	if s.agentHandler == nil {
		return
	}
	s.harnessRepliesMu.Lock()
	if s.harnessReplies == nil {
		s.harnessReplies = make(map[string]harnessReply)
	}
	if _, exists := s.harnessReplies[in.RunID]; exists {
		s.harnessRepliesMu.Unlock()
		return
	}
	s.harnessReplies[in.RunID] = harnessReply{agentID: in.AgentID, runID: in.RunID, idempotencyKey: in.IdempotencyKey,
		webChat: in.Channel == "web", created: in.ExpiresAt.Add(-15 * time.Minute), restored: true, completedResult: in.ResultID != ""}
	s.harnessRepliesMu.Unlock()
	s.agentHandler.MarkHarnessResponseRun(in.RunID, in.Channel == "web", false)
	s.agentHandler.MarkHarnessRestoredRun(in.RunID)
}
