package server

import (
	"strings"
	"testing"

	"go.autonomous.ai/os/system/harness"
)

func TestHarnessPreparationTextObservedStates(t *testing.T) {
	for _, tc := range []struct{ state, phase, want string }{
		{"accepted", "accepted", "accepted agent preparation"},
		{"running", "doctor", "preparation: doctor"},
		{"ready", "complete", "authentication and task success remain unverified"},
		{"failed", "install", "preparation failed"},
		{"needs_user_action", "launch", "needs user action"},
	} {
		t.Run(tc.state, func(t *testing.T) {
			got := harnessPreparationText(map[string]any{"state": tc.state, "phase": tc.phase})
			if !strings.Contains(got, tc.want) || !strings.Contains(got, "task has not been sent") {
				t.Fatalf("misleading progress: %q", got)
			}
		})
	}
	if got := harnessPreparationText(map[string]any{"state": "unexpected"}); got != "" {
		t.Fatalf("unknown state: %q", got)
	}
}

func TestHarnessPreparationGuidanceIsBoundedReportedContent(t *testing.T) {
	got := harnessPreparationText(map[string]any{
		"state":    "needs_user_action",
		"error":    map[string]any{"code": "FUTURE_CODE", "message": "login\x00required"},
		"guidance": strings.Repeat("界", 4000),
		"doctor":   []any{"DO NOT COPY RAW DOCTOR"},
	})
	if !strings.Contains(got, "[FUTURE_CODE] Harness reports: login required") || !strings.Contains(got, "Harness guidance:") {
		t.Fatalf("missing remote evidence: %q", got)
	}
	if len([]rune(got)) > 2300 || strings.Contains(got, "DO NOT COPY") || strings.ContainsRune(got, '\x00') {
		t.Fatal("unbounded/raw diagnostic leaked")
	}
}

func TestHarnessPreparationLocalMetadataIsStripped(t *testing.T) {
	for _, kind := range []string{"agent.prepare", "operation.get"} {
		frame := harness.Frame{"type": kind, "response": map[string]any{"run_id": "device-chat-42", "channel": "web"}, "operationId": "op"}
		reply, err := extractHarnessReply(frame)
		if err != nil || reply == nil || reply.RunID != "device-chat-42" {
			t.Fatalf("reply = %+v, error = %v", reply, err)
		}
		if _, exists := frame["response"]; exists {
			t.Fatal("local response metadata leaked to strict remote schema")
		}
		if frame["type"] != kind || frame["operationId"] != "op" {
			t.Fatal("remote fields changed")
		}
	}
}
