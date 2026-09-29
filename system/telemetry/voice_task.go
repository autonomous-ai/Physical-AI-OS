package telemetry

import (
	"crypto/rand"
	"time"
)

// ReportTaskLifecycleEnd reports a lifecycle end, distinguishing completion from aborted/error.
func ReportTaskLifecycleEnd(runID string, aborted, hasError bool) {
	if aborted || hasError {
		ReportTaskExecution(runID, "", "failed", "lifecycle_end_error")
		return
	}
	ReportTaskExecution(runID, "", "completed", "lifecycle_end")
}

// TaskGroup classifies a task source; internal notifications return "".
func TaskGroup(eventType string) string {
	switch eventType {
	case "voice", "voice_command", "voice_followup":
		return "voice"
	case "web_chat", "mqtt_chat":
		return "chat"
	case "", "voice_agent_handled", "voice_listening", "voice_listening_end", "look.capture":
		return ""
	default:
		return "sensing"
	}
}

// ReportTaskStarted records a source-specific start; repeating with runID binds the same turn.
func ReportTaskStarted(eventType, interactionID, runID string) string {
	group := TaskGroup(eventType)
	if group == "" {
		return interactionID
	}
	if interactionID == "" {
		if group == "sensing" && runID != "" {
			// A requeued dispatch keeps the same cohort identity.
			interactionID = "os-sensing-" + runID
		} else {
			interactionID = "os-" + group + "-" + rand.Text()
		}
	}
	Report(Event{
		Name: group + "_metrics_task_started",
		ID:   "vts-" + rand.Text(),
		Params: map[string]any{
			"schema_version":     1,
			"interaction_id":     interactionID,
			"run_id":             runID,
			"event_type":         eventType,
			"task_started_at_ms": time.Now().UnixMilli(),
		},
	})
	return interactionID
}

// ReportTaskExecution records an execution boundary (not answer correctness), joined to
// its start cohort by run_id or interaction_id.
func ReportTaskExecution(runID, interactionID, outcome, evidence string) {
	if runID == "" && interactionID == "" {
		return
	}
	// Keep the payload content-free: no errors, transcripts or tool results.
	switch evidence {
	case "harness_correlated_summary":
		if outcome != "completed" && outcome != "failed" && outcome != "cancelled" {
			return
		}
	case "lifecycle_end", "local_intent_returned", "chat_final_no_lifecycle", "harness_turn_done", "harness_turn_summary":
		if outcome != "completed" {
			return
		}
	case "lifecycle_error", "lifecycle_end_error", "chat_error", "local_intent_error", "dispatch_error", "harness_turn_error":
		if outcome != "failed" {
			return
		}
	case "lifecycle_error_recovered", "execution_observation_lost", "harness_delegated", "harness_question_open":
		if outcome != "unknown" {
			return
		}
	default:
		return
	}
	Report(Event{
		Name: "voice_metrics_task_execution",
		ID:   "vte-" + rand.Text(),
		Params: map[string]any{
			"schema_version":  1,
			"run_id":          runID,
			"interaction_id":  interactionID,
			"outcome":         outcome,
			"evidence":        evidence,
			"error":           outcome != "completed",
			"execution_at_ms": time.Now().UnixMilli(),
		},
	})
}

// ReportTaskObservationLost records discarded unfinished transport correlations.
func ReportTaskObservationLost(runIDs ...string) {
	seen := make(map[string]bool, len(runIDs))
	for _, runID := range runIDs {
		if runID == "" || seen[runID] {
			continue
		}
		seen[runID] = true
		ReportTaskExecution(runID, "", "unknown", "execution_observation_lost")
	}
}
