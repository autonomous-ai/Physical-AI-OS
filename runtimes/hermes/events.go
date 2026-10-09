package hermes

import (
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/sensingmsg"
	"go.autonomous.ai/os/system/lib/speakergate"
	"go.autonomous.ai/os/system/skillcontext/mood"
	"go.autonomous.ai/os/system/telemetry"
)

// pendingEvent is a sensing event buffered while the agent was busy.
type pendingEvent struct {
	eventType   string
	msg         string
	images      []string
	queuedAt    time.Time
	currentUser string
	fixedRunID  string
}

const busyTTL = 5 * time.Minute

// mergeDrainEnabled collapses surviving ambient sensing events into one turn.
const mergeDrainEnabled = true

// mergedSensingHeader frames a batched drain so the agent treats the joined lines as combined context for a single response instead of separate commands.
const mergedSensingHeader = "[ambient signals batched while busy — respond once, using the items below as combined context]\n\n"

// standaloneDrain reports whether an event keeps its own turn (voice commands, voice_agent_handled, images).
func standaloneDrain(ev pendingEvent) bool {
	if len(ev.images) > 0 {
		return true
	}
	switch ev.eventType {
	case "voice", "voice_command", "voice_agent_handled", "voice_followup", "web_chat", "mqtt_chat", "environment.update":
		return true
	}
	return false
}

// IsBusy mirrors openclaw.HermesService.IsBusy: true while a turn is in flight OR a chat.send is still waiting for response.created.
func (s *HermesService) IsBusy() bool {
	if s.inFlightStreams.Load() > 0 {
		return true
	}
	if s.activeTurn.Load() {
		since := s.busySince.Load()
		if since > 0 && time.Since(time.UnixMilli(since)) > busyTTL {
			slog.Warn("busy flag expired — auto-clearing (response.completed likely missed)",
				"component", "hermes", "stuck_for_s", int(time.Since(time.UnixMilli(since)).Seconds()))
			s.activeTurn.Store(false)
			go s.drainPendingEvents()
			return s.HasFreshPendingChatSend()
		}
		return true
	}
	return s.HasFreshPendingChatSend()
}

// SetBusy flips active state.
func (s *HermesService) SetBusy(busy bool) {
	if !busy && s.inFlightStreams.Load() > 0 {
		return
	}
	if busy {
		s.busySince.Store(time.Now().UnixMilli())
	}
	s.activeTurn.Store(busy)
	if !busy {
		s.drainPendingEvents()
	}
}

func (s *HermesService) QueuePendingEvent(eventType, msg string, images []string, fixedRunID string) {
	now := time.Now()
	curUser := mood.CurrentUser()
	if curUser == "" {
		curUser = "unknown"
	}
	s.pendingEventsMu.Lock()
	s.pendingEvents = append(s.pendingEvents, pendingEvent{eventType: eventType, msg: msg, images: images, queuedAt: now, currentUser: curUser, fixedRunID: fixedRunID})
	s.pendingEventsMu.Unlock()
	slog.Info("sensing event queued — agent busy", "component", "sensing", "type", eventType, "runId", fixedRunID)

	s.monitorBus.Push(domain.MonitorEvent{
		Type:    "sensing_queued",
		Summary: "[" + eventType + "] " + msg,
		Detail:  map[string]any{"type": eventType, "reason": "agent_busy"},
	})
}

// drainPendingEvents replays buffered sensing events.
func (s *HermesService) DrainPendingEvents() {
	s.drainPendingEvents()
}

func (s *HermesService) drainPendingEvents() {
	s.drainMu.Lock()
	defer s.drainMu.Unlock()
	if !s.ready.Load() || s.inFlightStreams.Load() > 0 {
		return
	}
	s.pendingEventsMu.Lock()
	events := s.pendingEvents
	s.pendingEvents = nil
	s.pendingEventsMu.Unlock()

	if len(events) == 0 {
		return
	}

	replayTypes := make([]string, len(events))
	for i, ev := range events {
		replayTypes[i] = ev.eventType
	}
	if speakergate.DeferReplay(replayTypes, s.drainPendingEvents) {
		s.pendingEventsMu.Lock()
		s.pendingEvents = append(events, s.pendingEvents...)
		s.pendingEventsMu.Unlock()
		return
	}

	sort.SliceStable(events, func(i, j int) bool {
		iv := events[i].eventType == "voice" || events[i].eventType == "voice_command"
		jv := events[j].eventType == "voice" || events[j].eventType == "voice_command"
		return iv && !jv
	})

	const expireAfter = 60 * time.Second
	expirable := map[string]bool{
		"environment.update":      true,
		"motion.activity":         true,
		"emotion.detected":        true,
		"speech_emotion.detected": true,
		"presence.enter":          true,
		"presence.leave":          true,
		"presence.away":           true,
	}
	filtered := events[:0]
	for _, ev := range events {
		if !sensingmsg.ReplayAllowed(ev.eventType) {
			slog.Info("environment event dropped at replay", "component", "sensing", "reason", "sleeping or capability unavailable")
			continue
		}
		if expirable[ev.eventType] && time.Since(ev.queuedAt) > expireAfter {
			slog.Info("sensing event expired from queue", "component", "sensing", "type", ev.eventType, "age_s", int(time.Since(ev.queuedAt).Seconds()))
			continue
		}
		filtered = append(filtered, ev)
	}
	events = filtered

	coalesce := map[string]bool{
		"environment.update":      true,
		"presence.enter":          true,
		"presence.leave":          true,
		"presence.away":           true,
		"motion.activity":         true,
		"emotion.detected":        true,
		"speech_emotion.detected": true,
	}
	lastIdx := make(map[string]int, len(events))
	for i, ev := range events {
		if coalesce[ev.eventType] {
			lastIdx[ev.eventType] = i
		}
	}
	if len(lastIdx) > 0 {
		dropped := 0
		coalesced := events[:0]
		for i, ev := range events {
			if coalesce[ev.eventType] && lastIdx[ev.eventType] != i {
				dropped++
				continue
			}
			coalesced = append(coalesced, ev)
		}
		if dropped > 0 {
			slog.Info("sensing events coalesced — kept latest only", "component", "sensing", "dropped", dropped, "remaining", len(coalesced))
		}
		events = coalesced
	}

	if len(events) == 0 {
		slog.Info("all pending sensing events expired, nothing to drain", "component", "sensing")
		return
	}

	slog.Info("draining pending sensing events", "component", "sensing", "count", len(events))

	if !mergeDrainEnabled {
		for _, ev := range events {
			s.sendOnePending(ev)
		}
		return
	}

	// Partition: standalone events (voice commands, silent replays, images) keep their own turn; the rest are pure-ambient sensing collapsed into one turn.
	var mergeable []pendingEvent
	for i, ev := range events {
		if standaloneDrain(ev) {
			remaining := append([]pendingEvent(nil), events[:i]...)
			remaining = append(remaining, events[i+1:]...)
			s.restoreUnsent(remaining)
			s.sendOnePending(ev)
			return
		}
		mergeable = append(mergeable, ev)
	}
	switch len(mergeable) {
	case 0:
	case 1:
		s.sendOnePending(mergeable[0])
	default:
		s.sendMergedPending(mergeable)
	}
}

// sendOnePending replays one buffered event as its own turn.
func (s *HermesService) sendOnePending(ev pendingEvent) {
	var reqID, runID string
	if ev.fixedRunID != "" {
		reqID = ev.fixedRunID
		runID = ev.fixedRunID
	} else {
		reqID, runID = s.NextChatRunID()
	}
	hal.RegisterTurnSpeechPolicy(runID, speakergate.WaitsForSpeaker(ev.eventType))
	flow.SetTrace(runID)
	startPayload := map[string]any{"type": ev.eventType, "message": ev.msg}
	if !ev.queuedAt.IsZero() {
		startPayload["queued_for_ms"] = time.Since(ev.queuedAt).Milliseconds()
		startPayload["queued_at"] = ev.queuedAt.Unix()
	}
	turnStart := flow.Start("sensing_input", startPayload, runID)

	if ev.eventType == "motion.activity" {
		if bid, worst := extractPoseBucketMarkers(ev.msg); bid != "" {
			s.MarkPoseBucketRun(runID, bid, worst)
		}
	}
	msg := sensingmsg.Build(ev.eventType, ev.msg, ev.currentUser, "")
	msg = reSnapshotPath.ReplaceAllString(msg, "")
	msg = rePoseBucketMarker.ReplaceAllString(msg, "")
	msg = rePoseWorstMarker.ReplaceAllString(msg, "")
	msg = strings.ReplaceAll(msg, "\n\n\n", "\n\n")
	msg = strings.TrimSpace(msg)
	msg = sensingmsg.AppendHarnessReplyRoute(msg, ev.eventType, runID)

	if ev.eventType == "voice_agent_handled" {
		s.MarkSilentRun(runID)
	}

	var err error
	if len(ev.images) > 0 {
		_, err = s.SendChatMessageWithImagesAndRun(msg, ev.images, reqID, runID)
	} else {
		_, err = s.SendChatMessageWithRun(msg, reqID, runID)
	}
	if telemetry.TaskGroup(ev.eventType) == "sensing" && !errors.Is(err, errHermesNotReady) {
		telemetry.ReportTaskStarted(ev.eventType, "", runID)
	}
	if err != nil {
		if errors.Is(err, errHermesNotReady) {
			s.restoreUnsent([]pendingEvent{ev})
		} else if telemetry.TaskGroup(ev.eventType) != "" {
			telemetry.ReportTaskExecution(runID, "", "failed", "dispatch_error")
		}
		slog.Error("failed to replay pending event", "component", "sensing", "type", ev.eventType, "error", err)
		flow.End("sensing_input", turnStart, map[string]any{"error": err.Error()}, runID)
		return
	}
	flow.End("sensing_input", turnStart, map[string]any{"path": "agent", "run_id": runID}, runID)
	flow.Log("agent_call", map[string]any{"type": ev.eventType, "run_id": runID}, runID)
	slog.Info("pending event replayed", "component", "sensing", "type", ev.eventType, "runId", runID)
}

// sendMergedPending collapses multiple ambient sensing events into one turn under one runID.
func (s *HermesService) sendMergedPending(evs []pendingEvent) {
	reqID, runID := s.NextChatRunID()
	flow.SetTrace(runID)

	for _, ev := range evs {
		if ev.eventType == "motion.activity" {
			if bid, worst := extractPoseBucketMarkers(ev.msg); bid != "" {
				s.MarkPoseBucketRun(runID, bid, worst)
			}
		}
	}

	merged, types, oldest := buildMergedSensing(evs)
	if merged == "" {
		return
	}
	count := len(types)

	startPayload := map[string]any{"types": types, "merged": true, "count": count}
	if !oldest.IsZero() {
		startPayload["queued_for_ms"] = time.Since(oldest).Milliseconds()
		startPayload["queued_at"] = oldest.Unix()
	}
	turnStart := flow.Start("sensing_input", startPayload, runID)

	s.monitorBus.Push(domain.MonitorEvent{
		Type:    "sensing_drain_merged",
		Summary: fmt.Sprintf("%d sensing events → 1 turn", count),
		Detail:  map[string]any{"types": types, "count": count},
		RunID:   runID,
	})

	passive := true
	for _, eventType := range types {
		if !speakergate.WaitsForSpeaker(eventType) {
			passive = false
			break
		}
	}
	hal.RegisterTurnSpeechPolicy(runID, passive)
	_, err := s.SendChatMessageWithRun(merged, reqID, runID)
	if !errors.Is(err, errHermesNotReady) {
		telemetry.ReportTaskStarted("sensing_drain_merged", "", runID)
	}
	if err != nil {
		if errors.Is(err, errHermesNotReady) {
			s.restoreUnsent(evs)
		} else {
			telemetry.ReportTaskExecution(runID, "", "failed", "dispatch_error")
		}
		slog.Error("failed to replay merged sensing events", "component", "sensing", "types", types, "error", err)
		flow.End("sensing_input", turnStart, map[string]any{"error": err.Error()}, runID)
		return
	}
	flow.End("sensing_input", turnStart, map[string]any{"path": "agent", "run_id": runID, "merged": count}, runID)
	flow.Log("agent_call", map[string]any{"types": types, "run_id": runID, "merged": count}, runID)
	slog.Info("merged sensing events replayed", "component", "sensing", "types", types, "count", count, "runId", runID)
}

// buildMergedSensing builds the merged chat message from ambient sensing events.
func buildMergedSensing(evs []pendingEvent) (merged string, types []string, oldest time.Time) {
	types = make([]string, 0, len(evs))
	parts := make([]string, 0, len(evs))
	for _, ev := range evs {
		m := sensingmsg.Build(ev.eventType, ev.msg, ev.currentUser, "")
		m = reSnapshotPath.ReplaceAllString(m, "")
		m = rePoseBucketMarker.ReplaceAllString(m, "")
		m = rePoseWorstMarker.ReplaceAllString(m, "")
		m = strings.TrimSpace(m)
		if m == "" {
			continue
		}
		parts = append(parts, m)
		types = append(types, ev.eventType)
		if !ev.queuedAt.IsZero() && (oldest.IsZero() || ev.queuedAt.Before(oldest)) {
			oldest = ev.queuedAt
		}
	}
	if len(parts) == 0 {
		return "", types, oldest
	}
	merged = mergedSensingHeader + strings.Join(parts, "\n\n")
	merged = strings.ReplaceAll(merged, "\n\n\n", "\n\n")
	merged = strings.TrimSpace(merged)
	return merged, types, oldest
}

// Only synchronous pre-send rejection is safe to retain; never retry an attempted POST.
func (s *HermesService) restoreUnsent(events []pendingEvent) {
	s.pendingEventsMu.Lock()
	s.pendingEvents = append(events, s.pendingEvents...)
	s.pendingEventsMu.Unlock()
}
