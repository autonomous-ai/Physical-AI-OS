package server

import (
	"testing"

	"go.autonomous.ai/os/system/harness"
)

func permissionFrame(kind, questionID string, permission bool) harness.Frame {
	payload := map[string]any{"questionRequestId": questionID, "questions": []any{map[string]any{"key": "Run?", "q": "Run?"}}}
	if permission {
		payload["permission"] = map[string]any{"dialog": "Run?", "resolution": "desktop"}
	}
	return harness.Frame{"type": "event", "kind": kind, "machineId": "m", "agentId": "a", "payload": payload}
}

func TestHarnessPermissionNoticeOncePerOpenDialog(t *testing.T) {
	s := &Server{}
	if s.handleHarnessPermission(permissionFrame("question.open", "q1", false)) {
		t.Fatal("ordinary question must keep the existing question path")
	}
	if !s.handleHarnessPermission(permissionFrame("question.open", "q1", true)) {
		t.Fatal("permission dialog reached the answerable question path")
	}
	// Replay or reconnect of the same open dialog must not notify again.
	s.handleHarnessPermission(permissionFrame("question.open", "q1", true))
	if len(s.harnessPermissionNotified) != 1 {
		t.Fatalf("notified = %v", s.harnessPermissionNotified)
	}
	if s.handleHarnessPermission(permissionFrame("question.close", "q1", false)) {
		t.Fatal("close must stay on the existing path")
	}
	if len(s.harnessPermissionNotified) != 0 {
		t.Fatal("matching close did not clear the notice")
	}
	if missing := (harness.Frame{"kind": "question.open", "payload": map[string]any{"permission": map[string]any{"dialog": "x", "resolution": "desktop"}}}); !s.handleHarnessPermission(missing) {
		t.Fatal("permission dialog without identity must still be withheld from answering")
	}
	if got := harnessPermissionNoticeText(""); got != "A Harness agent needs permission. Open OpenHarness to review and approve or deny." {
		t.Fatalf("notice = %q", got)
	}
}
