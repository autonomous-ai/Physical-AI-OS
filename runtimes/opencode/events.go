package opencode

import (
	"errors"
	"log/slog"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
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

// busyTTL bounds how long the busy flag survives without a terminal frame.
// It must stay longer than the gatewayd per-turn timeout, or slow turns lose their run id.
const busyTTLMargin = 5 * time.Minute

func busyTTL() time.Duration {
	// Matches the gatewayd default turn timeout.
	timeout := 10 * time.Minute
	if f, err := strconv.ParseFloat(strings.TrimSpace(os.Getenv("OPENCODE_TURN_TIMEOUT_S")), 64); err == nil && f > 0 {
		timeout = time.Duration(f * float64(time.Second))
	}
	return timeout + busyTTLMargin
}

// IsBusy mirrors openclaw's OpenclawService.IsBusy: true while a turn is in flight OR a
// chat.send is still waiting for its first inbound frame.
func (s *OpenCodeService) IsBusy() bool {
	if s.activeTurn.Load() {
		since := s.busySince.Load()
		if since > 0 && time.Since(time.UnixMilli(since)) > busyTTL() {
			slog.Warn("busy flag expired — auto-clearing (final frame likely missed)",
				"component", "opencode", "stuck_for_s", int(time.Since(time.UnixMilli(since)).Seconds()))
			s.activeTurn.Store(false)
			s.clearTurn()
			go s.drainPendingEvents()
			return s.HasFreshPendingChatSend()
		}
		return true
	}
	return s.HasFreshPendingChatSend()
}

// SetBusy flips active state.
func (s *OpenCodeService) SetBusy(busy bool) {
	if !busy && (s.getCurrentRunID() != "" || s.hasPendingRuns()) {
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

func (s *OpenCodeService) QueuePendingEvent(eventType, msg string, images []string, fixedRunID string) {
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

// DrainPendingEvents replays buffered sensing events; it also runs when the speaker frees up.
func (s *OpenCodeService) DrainPendingEvents() {
	s.drainPendingEvents()
}

func (s *OpenCodeService) drainPendingEvents() {
	s.pendingEventsDrainMu.Lock()
	defer s.pendingEventsDrainMu.Unlock()
	if !s.wsConnected.Load() {
		return
	}
	s.pendingEventsMu.Lock()
	events := s.pendingEvents
	s.pendingEvents = nil
	s.pendingEventsMu.Unlock()

	if len(events) == 0 {
		return
	}

	// Put the batch back while the last reply is still playing; replaying now would cut it off.
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
	for i, ev := range events {
		if !s.wsConnected.Load() {
			s.pendingEventsMu.Lock()
			s.pendingEvents = append(events[i:], s.pendingEvents...)
			s.pendingEventsMu.Unlock()
			return
		}
		var reqID, runID string
		if ev.fixedRunID != "" {
			reqID = ev.fixedRunID
			runID = ev.fixedRunID
		} else {
			reqID, runID = s.NextChatRunID()
		}
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

		if telemetry.TaskGroup(ev.eventType) == "sensing" {
			ev.fixedRunID = runID
			events[i] = ev
			telemetry.ReportTaskStarted(ev.eventType, "", runID)
		}
		var err error
		if len(ev.images) > 0 {
			_, err = s.SendChatMessageWithImagesAndRun(msg, ev.images, reqID, runID)
		} else {
			_, err = s.SendChatMessageWithRun(msg, reqID, runID)
		}
		if err != nil {
			if errors.Is(err, errDisconnectedBeforeSend) {
				s.RemovePendingChatTraceByRunID(runID)
				s.pendingEventsMu.Lock()
				s.pendingEvents = append(events[i:], s.pendingEvents...)
				s.pendingEventsMu.Unlock()
				flow.End("sensing_input", turnStart, map[string]any{"deferred": "disconnected before send"}, runID)
				return
			}
			if telemetry.TaskGroup(ev.eventType) != "" {
				telemetry.ReportTaskExecution(runID, "", "failed", "dispatch_error")
			}
			slog.Error("failed to replay pending event", "component", "sensing", "type", ev.eventType, "error", err)
			flow.End("sensing_input", turnStart, map[string]any{"error": err.Error()}, runID)
		} else {
			flow.End("sensing_input", turnStart, map[string]any{"path": "agent", "run_id": runID}, runID)
			flow.Log("agent_call", map[string]any{"type": ev.eventType, "run_id": runID}, runID)
			slog.Info("pending event replayed", "component", "sensing", "type", ev.eventType, "runId", runID)
		}
	}
}
