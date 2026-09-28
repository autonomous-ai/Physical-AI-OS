package telemetry

import (
	"encoding/json"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
)

func TestTaskLifecycleEndRejectsAbortedAndErrorFrames(t *testing.T) {
	cases := []struct {
		name, data, outcome, evidence string
	}{
		{"completed", `{"phase":"end","aborted":false,"stopReason":"stop"}`, "completed", "lifecycle_end"},
		{"aborted", `{"phase":"end","aborted":true}`, "failed", "lifecycle_end_error"},
		{"error", `{"phase":"end","error":"private runtime error"}`, "failed", "lifecycle_end_error"},
		{"both", `{"phase":"end","aborted":true,"error":"runtime error"}`, "failed", "lifecycle_end_error"},
		{"legacy", `{"phase":"end"}`, "completed", "lifecycle_end"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := newMock(nil, 1)
			withPipe(t, m)
			var payload domain.AgentPayload
			if err := json.Unmarshal([]byte(`{"runId":"device-terminal","stream":"lifecycle","data":`+tc.data+`}`), &payload); err != nil {
				t.Fatal(err)
			}
			ReportTaskLifecycleEnd(payload.RunID, payload.Data.Aborted, payload.Data.Error != "")
			m.wait(t, 1)
			params := m.events[0].params
			if params["outcome"] != tc.outcome || params["evidence"] != tc.evidence || params["error"] != (tc.outcome == "failed") {
				t.Fatalf("incorrect terminal verdict: %+v", params)
			}
		})
	}
}

func TestTaskExecutionPreservesEvidenceAndCorrelation(t *testing.T) {
	m := newMock(nil, 7)
	withPipe(t, m)
	start := time.Now().UnixMilli()
	cases := []struct{ run, interaction, outcome, evidence string }{
		{"device-1", "", "completed", "lifecycle_end"},
		{"device-2", "", "failed", "lifecycle_error"},
		{"device-3", "", "unknown", "lifecycle_error_recovered"},
		{"device-4", "", "failed", "chat_error"},
		{"", "vi-local", "completed", "local_intent_returned"},
		{"", "vi-local-failed", "failed", "local_intent_error"},
		{"device-chat-slash", "", "completed", "chat_final_no_lifecycle"},
	}
	for _, tc := range cases {
		ReportTaskExecution(tc.run, tc.interaction, tc.outcome, tc.evidence)
	}
	m.wait(t, len(cases))
	seen := map[string]bool{}
	for i, tc := range cases {
		got := m.events[i]
		if got.name != "voice_metrics_task_execution" {
			t.Fatalf("event name = %q", got.name)
		}
		for key, want := range map[string]any{
			"run_id": tc.run, "interaction_id": tc.interaction,
			"outcome": tc.outcome, "evidence": tc.evidence,
			"schema_version": 1, "error": tc.outcome != "completed",
		} {
			if got.params[key] != want {
				t.Errorf("%s[%s] = %v, want %v", tc.evidence, key, got.params[key], want)
			}
		}
		stamp, ok := got.params["execution_at_ms"].(int64)
		if !ok || stamp < start || stamp > time.Now().UnixMilli() {
			t.Errorf("invalid execution timestamp: %v", got.params["execution_at_ms"])
		}
		id, _ := got.params["event_id"].(string)
		if id == "" || seen[id] {
			t.Errorf("missing or duplicate event ID %q", id)
		}
		seen[id] = true
	}
}

func TestTaskExecutionRejectsUncorrelatedAndUnboundedValues(t *testing.T) {
	m := newMock(nil, 1)
	withPipe(t, m)
	ReportTaskExecution("", "", "completed", "lifecycle_end")
	ReportTaskExecution("device-1", "", "sensitive error text", "lifecycle_error")
	ReportTaskExecution("device-1", "", "failed", "sensitive tool result")
	ReportTaskExecution("device-1", "", "completed", "lifecycle_error_recovered")
	ReportTaskExecution("device-1", "", "completed", "local_intent_error")
	ReportTaskExecution("device-1", "", "completed", "lifecycle_end_error")
	ReportTaskExecution("device-1", "", "failed", "chat_final_no_lifecycle")
	ReportTaskExecution("device-1", "", "unknown", "chat_final_no_lifecycle")
	ReportTaskExecution("device-1", "", "completed", "lifecycle_end")
	m.wait(t, 1)
	if len(m.events) != 1 || m.events[0].params["evidence"] != "lifecycle_end" {
		t.Fatalf("unexpected observations: %+v", m.events)
	}
}

func TestVoiceTaskStartsPreserveOwnershipAndExcludeNonTasks(t *testing.T) {
	m := newMock(nil, 3)
	withPipe(t, m)
	id := ReportTaskStarted("voice_followup", "", "")
	if id == "" {
		t.Fatal("direct voice request must receive an interaction ID")
	}
	if got := ReportTaskStarted("voice_followup", id, "main-run"); got != id {
		t.Fatal("binding changed the turn identity")
	}
	ReportTaskStarted("voice_command", "vi-from-hal", "local-run")
	for _, typ := range []string{"voice_agent_handled", "voice_listening", "voice_listening_end", "look.capture"} {
		ReportTaskStarted(typ, "", "background-run")
	}
	m.wait(t, 3)
	if len(m.events) != 3 {
		t.Fatalf("non-task event entered denominator: %d", len(m.events))
	}
	for i, event := range m.events {
		if event.name != "voice_metrics_task_started" || event.params["schema_version"] != 1 {
			t.Fatalf("invalid start event: %+v", event)
		}
		if _, ok := event.params["task_started_at_ms"].(int64); !ok {
			t.Fatal("missing start timestamp")
		}
		wantID := id
		if i == 2 {
			wantID = "vi-from-hal"
		}
		if event.params["interaction_id"] != wantID {
			t.Fatalf("wrong ownership: %+v", event.params)
		}
	}
	if m.events[1].params["run_id"] != "main-run" {
		t.Fatal("missing main-agent correlation")
	}
}

func TestTaskStartsSeparateChatSensingAndVoice(t *testing.T) {
	m := newMock(nil, 5)
	withPipe(t, m)
	cases := []struct{ eventType, group string }{
		{"voice_followup", "voice"}, {"web_chat", "chat"}, {"mqtt_chat", "chat"},
		{"motion.activity", "sensing"}, {"presence.enter", "sensing"},
	}
	for _, tc := range cases {
		if got := TaskGroup(tc.eventType); got != tc.group {
			t.Fatalf("%s grouped as %s", tc.eventType, got)
		}
		ReportTaskStarted(tc.eventType, "", "run-"+tc.eventType)
	}
	m.wait(t, len(cases))
	for i, tc := range cases {
		if m.events[i].name != tc.group+"_metrics_task_started" {
			t.Fatalf("wrong source event: %+v", m.events[i])
		}
		if tc.group == "sensing" && m.events[i].params["interaction_id"] != "os-sensing-run-"+tc.eventType {
			t.Fatalf("sensing retry identity must be deterministic: %+v", m.events[i].params)
		}
	}
}

func TestTaskObservationLossIsUnknownAndDeduplicatesRunIDs(t *testing.T) {
	m := newMock(nil, 2)
	withPipe(t, m)
	ReportTaskExecution("invalid", "", "completed", "execution_observation_lost")
	ReportTaskExecution("invalid", "", "failed", "execution_observation_lost")
	ReportTaskObservationLost("active", "", "queued", "active")
	m.wait(t, 2)
	if len(m.events) != 2 {
		t.Fatalf("unexpected observations: %+v", m.events)
	}
	for i, run := range []string{"active", "queued"} {
		params := m.events[i].params
		if params["run_id"] != run || params["outcome"] != "unknown" || params["evidence"] != "execution_observation_lost" {
			t.Fatalf("wrong lost observation: %+v", params)
		}
	}
}

func TestHarnessEvidenceOutcomesAreBounded(t *testing.T) {
	m := newMock(nil, 5)
	withPipe(t, m)
	cases := []struct{ evidence, outcome string }{
		{"harness_delegated", "unknown"}, {"harness_question_open", "unknown"},
		{"harness_turn_done", "completed"}, {"harness_turn_summary", "completed"},
		{"harness_turn_error", "failed"},
	}
	for _, tc := range cases {
		for _, outcome := range []string{"completed", "failed", "unknown"} {
			ReportTaskExecution("device-harness-test", "", outcome, tc.evidence)
		}
	}
	m.wait(t, 5)
	for i, event := range m.events {
		if event.params["evidence"] != cases[i].evidence || event.params["outcome"] != cases[i].outcome {
			t.Fatalf("wrong Harness evidence: %v", event.params)
		}
	}
}

func TestHarnessResultEvidencePreservesAllTerminalOutcomes(t *testing.T) {
	m := newMock(nil, 3)
	withPipe(t, m)
	for _, outcome := range []string{"unknown", "accepted", "delivered", "private result text", ""} {
		ReportTaskExecution("run", "", outcome, "harness_correlated_summary")
	}
	ReportTaskExecution("run", "", "cancelled", "private evidence text")
	for _, outcome := range []string{"completed", "failed", "cancelled"} {
		ReportTaskExecution("run-"+outcome, "", outcome, "harness_correlated_summary")
	}
	m.wait(t, 3)
	if len(m.events) != 3 {
		t.Fatalf("unexpected observations: %+v", m.events)
	}
	for i, outcome := range []string{"completed", "failed", "cancelled"} {
		p := m.events[i].params
		if p["outcome"] != outcome || p["evidence"] != "harness_correlated_summary" || p["error"] != (outcome != "completed") || p["run_id"] != "run-"+outcome {
			t.Fatalf("invalid result evidence: %+v", p)
		}
	}
}
