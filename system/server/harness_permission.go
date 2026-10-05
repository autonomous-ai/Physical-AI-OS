package server

import (
	"context"
	"log/slog"
	"strings"
	"time"

	"go.autonomous.ai/os/system/harness"
)

// Permission notices follow the OpenHarness "Permission notices (notification only)"
// contract: a recognized terminal approval dialog arrives as question.open carrying
// payload.permission. The device tells the user once to handle it in OpenHarness and
// never answers, reminds or approves; approval stays in Desktop/terminal.

// ponytail: the notified set is reset when full; a reset can repeat at most one notice per open dialog.
const harnessPermissionNoticeLimit = 256

// harnessPermissionDialog reports whether a question payload or status openQuestion is
// a permission dialog. Missing metadata (older Harness) means unknown, not safe.
func harnessPermissionDialog(question map[string]any) bool {
	permission, ok := question["permission"].(map[string]any)
	if !ok {
		return false
	}
	_, hasDialog := permission["dialog"].(string)
	return hasDialog && permission["resolution"] == "desktop"
}

func harnessPermissionIdentity(frame harness.Frame) (machineID, agentID, questionID string) {
	machineID, _ = frame["machineId"].(string)
	if machineID == "" {
		if provenance, ok := frame["_osResultContext"].(harness.ResultContext); ok {
			machineID = provenance.MachineID
		}
	}
	agentID, _ = frame["agentId"].(string)
	payload, _ := frame["payload"].(map[string]any)
	questionID, _ = payload["questionRequestId"].(string)
	return machineID, agentID, questionID
}

// handleHarnessPermission consumes permission question.open events and clears notice
// state on question.close. It returns true when the frame must not reach the ordinary
// question path.
func (s *Server) handleHarnessPermission(frame harness.Frame) bool {
	kind, _ := frame["kind"].(string)
	if kind != "question.open" && kind != "question.close" {
		return false
	}
	machineID, agentID, questionID := harnessPermissionIdentity(frame)
	key := machineID + "\x00" + agentID + "\x00" + questionID
	if kind == "question.close" {
		// Closure only clears pending state; it proves neither approval nor denial.
		s.harnessPermissionMu.Lock()
		delete(s.harnessPermissionNotified, key)
		s.harnessPermissionMu.Unlock()
		return false
	}
	payload, _ := frame["payload"].(map[string]any)
	if !harnessPermissionDialog(payload) {
		return false
	}
	if machineID == "" || agentID == "" || questionID == "" {
		return true
	}
	s.harnessPermissionMu.Lock()
	if s.harnessPermissionNotified[key] {
		s.harnessPermissionMu.Unlock()
		return true
	}
	if s.harnessPermissionNotified == nil || len(s.harnessPermissionNotified) >= harnessPermissionNoticeLimit {
		s.harnessPermissionNotified = map[string]bool{}
	}
	s.harnessPermissionNotified[key] = true
	s.harnessPermissionMu.Unlock()
	go s.announceHarnessPermission(frame, machineID, agentID, questionID)
	return true
}

// announceHarnessPermission speaks the notice only while live status still shows the
// same dialog, so a replayed open that already closed stays silent.
func (s *Server) announceHarnessPermission(frame harness.Frame, machineID, agentID, questionID string) {
	if s.harnessService == nil || s.agentHandler == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	status, err := s.harnessService.Request(ctx, harness.Frame{"type": "status", "machineId": machineID, "agentId": agentID})
	if err != nil {
		slog.Warn("Harness permission notice skipped: live status unavailable", "component", "harness", "agent_id", agentID, "error", err)
		return
	}
	open, _ := status["openQuestion"].(map[string]any)
	if open == nil || open["requestId"] != questionID || !harnessPermissionDialog(open) {
		return
	}
	text := harnessPermissionNoticeText(s.harnessAgentName(ctx, machineID, agentID))
	s.harnessRepliesMu.Lock()
	reply, ok := s.harnessReplyForFrameLocked(agentID, frame)
	if !ok {
		// The dialog belongs to the agent, and Harness omits or mismatches the turn key when
		// an older turn is still open; fall back to the agent's newest live device route.
		reply, ok = s.newestHarnessRouteLocked(agentID)
	}
	s.harnessRepliesMu.Unlock()
	route := "device"
	switch {
	case ok && reply.webChat:
		// Web/MQTT chat tasks show the notice in their chat only; the device never speaks it.
		route = "chat"
		if !s.agentHandler.DeliverHarnessQuestion(reply.runID, questionID, text) {
			route = "chat_refused"
		}
	case ok && s.agentHandler.DeliverHarnessQuestion(reply.runID, questionID, text):
		route = "voice"
	default:
		s.agentHandler.AnnounceHarnessNotice(text)
	}
	slog.Info("Harness permission notice", "component", "harness", "agent_id", agentID, "question_id", questionID, "route", route, "run_id", reply.runID)
}

// newestHarnessRouteLocked returns the agent's most recent live (15-minute) task route.
// ponytail: newest-wins heuristic; overlapping tasks on one agent cannot be told apart here.
func (s *Server) newestHarnessRouteLocked(agentID string) (harnessReply, bool) {
	var newest harnessReply
	found := false
	for _, reply := range s.harnessReplies {
		if reply.answer || reply.localOnly || reply.completedResult || reply.agentID != agentID || time.Since(reply.created) > 15*time.Minute {
			continue
		}
		if !found || reply.created.After(newest.created) {
			newest, found = reply, true
		}
	}
	return newest, found
}

// harnessAgentName resolves a display name from agents.list; empty when unavailable.
func (s *Server) harnessAgentName(ctx context.Context, machineID, agentID string) string {
	listing, err := s.harnessService.Request(ctx, harness.Frame{"type": "agents.list"})
	if err != nil || listing["machineId"] != machineID {
		return ""
	}
	agents, _ := listing["agents"].([]any)
	for _, raw := range agents {
		agent, _ := raw.(map[string]any)
		if agent["agentId"] != agentID {
			continue
		}
		name, _ := agent["name"].(string)
		name = strings.Join(strings.Fields(name), " ")
		if runes := []rune(name); len(runes) > 64 {
			name = string(runes[:64])
		}
		return name
	}
	return ""
}

func harnessPermissionNoticeText(agentName string) string {
	subject := "A Harness agent"
	if agentName != "" {
		subject = "Agent " + agentName
	}
	return subject + " needs permission. Open OpenHarness to review and approve or deny."
}
