package telemetry

import (
	"crypto/rand"
	"time"
)

// ReportTaskLifecycleEnd distinguishes normal completion from aborted/error
// terminal frames. A lifecycle "end" alone does not guarantee execution success.
func ReportTaskLifecycleEnd(runID string, aborted, hasError bool) {
	if aborted || hasError {
		ReportTaskExecution(runID, "", "failed", "lifecycle_end_error")
		return
	}
	ReportTaskExecution(runID, "", "completed", "lifecycle_end")
}

// TaskGroup classifies task sources, excluding internal notifications that do
// not request execution. Other sensing types are eligible only after routing
// policy selects them for dispatch; callers enforce that acceptance boundary.
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

// ReportTaskStarted records a source-specific denominator. Repeating it with
// the assigned run ID binds the same turn without adding another turn.
// Voice/chat receipt is measured immediately; sensing is measured only once
// selected for dispatch, after filtering and queued-event coalescing.
func ReportTaskStarted(eventType, interactionID, runID string) string {
	group := TaskGroup(eventType)
	if group == "" {
		return interactionID
	}
	if interactionID == "" {
		if group == "sensing" && runID != "" {
			// A requeued dispatch attempt retains the same cohort identity.
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

// ReportTaskExecution records an execution boundary, not answer correctness.
// The legacy event name is shared by voice, chat and sensing to avoid emitting
// duplicate terminal events. Consumers join to their source-specific start
// cohort by run_id or interaction_id; standalone backend runs are not scored.
func ReportTaskExecution(runID, interactionID, outcome, evidence string) {
	if runID == "" && interactionID == "" {
		return
	}
	// Keep this payload content-free even if a future caller supplies an
	// unexpected value: errors, transcripts and tool results must not escape.
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
// It does not assert that the remote execution failed or alter runtime lifecycle.
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
