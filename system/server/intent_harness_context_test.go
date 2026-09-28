package server

import "testing"

func TestHarnessPendingContextUsesResponseOwnership(t *testing.T) {
	s := &Server{}
	if s.HarnessTaskPending() {
		t.Fatal("empty server has no task context")
	}
	s.harnessReplies = map[string]harnessReply{"run": {agentID: "render-agent", runID: "run"}}
	if !s.HarnessTaskPending() {
		t.Fatal("pending task must outlive follow-up timer")
	}
	if !s.takeHarnessReply("render-agent", "run") || s.HarnessTaskPending() {
		t.Fatal("completed route retained pending task")
	}
}
