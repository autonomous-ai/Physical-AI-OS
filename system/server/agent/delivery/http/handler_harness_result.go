package http

import (
	"errors"
	"fmt"
	"sort"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
	"go.autonomous.ai/os/system/lib/hal"
	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
)

// ErrHarnessResultSpeechSuppressed identifies a local policy refusal before submission.
var ErrHarnessResultSpeechSuppressed = errors.New("Harness result speech suppressed")

// DeliverHarnessGroupedResult closes a validated result's member routes together: one answer
// plus references on the other runs; never speaks or fabricates a per-input answer.
func (h *AgentHandler) DeliverHarnessGroupedResult(resultID, outcome, text string, runIDs []string) bool {
	if resultID == "" || text == "" || len(runIDs) == 0 {
		return false
	}
	switch outcome {
	case "completed", "failed", "cancelled":
	default:
		return false
	}
	h.harnessRepliesMu.Lock()
	seen := make(map[string]bool, len(runIDs))
	for _, id := range runIDs {
		state, ok := h.harnessReplies[id]
		if id == "" || seen[id] || !ok || state.delivered || state.localOnly {
			h.harnessRepliesMu.Unlock()
			return false
		}
		seen[id] = true
	}
	runIDs = h.harnessResultRunOrderLocked(runIDs)
	for _, id := range runIDs {
		state := h.harnessReplies[id]
		state.delivered = true
		h.harnessReplies[id] = state
	}
	h.harnessRepliesMu.Unlock()
	for i, id := range runIDs {
		sensinghttp.DefaultFillerManager.Cancel(id)
		message := text
		if i != 0 {
			message = "See the shared reply for these requests."
		}
		details := map[string]string{"role": "assistant", "message": message, "source": "harness", "result_id": resultID, "outcome": outcome, "result_run_id": runIDs[0]}
		if i != 0 {
			details["result_reference"] = "true"
		}
		flow.Log("harness_response", map[string]any{"run_id": id, "text": message, "result_id": resultID, "result_run_id": runIDs[0], "outcome": outcome, "result_reference": i != 0}, id)
		if h.monitorBus != nil {
			h.monitorBus.Push(domain.MonitorEvent{Type: "chat_response", Summary: message, RunID: id, State: "final", Detail: details})
		}
	}
	return true
}

// harnessResultRunOrderLocked picks the presentation/speech owner by device timestamp, then
// registration time. Caller holds harnessRepliesMu.
func (h *AgentHandler) harnessResultRunOrderLocked(runIDs []string) []string {
	ordered := append([]string(nil), runIDs...)
	times := make(map[string]int64, len(ordered))
	for _, id := range ordered {
		times[id] = h.runCreatedAtMs(id)
	}
	sort.SliceStable(ordered, func(i, j int) bool {
		a, b := ordered[i], ordered[j]
		if times[a] != times[b] {
			return times[a] > times[b]
		}
		if h.harnessReplies[a].created != h.harnessReplies[b].created {
			return h.harnessReplies[a].created.After(h.harnessReplies[b].created)
		}
		return a < b
	})
	return ordered
}

// SpeakHarnessGroupedResult submits exactly one synchronous HAL request (nil = accepted, not played).
// Caller must claim its durable outbox first and must not retry an uncertain submission.
func (h *AgentHandler) SpeakHarnessGroupedResult(text, outcome string, runIDs []string) error {
	return h.speakHarnessGroupedResult(text, runIDs, func(text, owner string) error {
		return hal.AnnounceHarnessUpdate(hal.HarnessUpdateResult, text, owner, outcome)
	})
}

func (h *AgentHandler) speakHarnessGroupedResult(text string, runIDs []string, send func(string, string) error) error {
	if text == "" || len(runIDs) == 0 {
		return fmt.Errorf("%w: no speech or members", ErrHarnessResultSpeechSuppressed)
	}
	h.harnessRepliesMu.Lock()
	seen := make(map[string]bool, len(runIDs))
	for _, id := range runIDs {
		state, ok := h.harnessReplies[id]
		if id == "" || seen[id] || !ok || state.webChat || state.localOnly || state.restored || time.Since(state.created) > 15*time.Minute {
			h.harnessRepliesMu.Unlock()
			return fmt.Errorf("%w: member %q is not an eligible voice route", ErrHarnessResultSpeechSuppressed, id)
		}
		seen[id] = true
	}
	runIDs = h.harnessResultRunOrderLocked(runIDs)
	h.harnessRepliesMu.Unlock()
	owner := runIDs[0]
	// The merged answer belongs to the newest input's speech turn; cancelling an older
	// member must not cancel it.
	if h.isHarnessSpeechCancelled(owner) {
		flow.Log("tts_cancelled", map[string]any{"run_id": owner, "source": h.speechCancelSource(owner)}, owner)
		return fmt.Errorf("%w: latest member %q lost the speaker", ErrHarnessResultSpeechSuppressed, owner)
	}
	// Correlated results are immutable; never replace them with a local notice.
	if isLLMLimitText(text) {
		return fmt.Errorf("%w: usage-limit banner is display-only", ErrHarnessResultSpeechSuppressed)
	}
	defer hal.BeginVoiceFollowupSpeech(owner)()
	err := send(text, owner)
	if errors.Is(err, hal.ErrSpeakerMuted) {
		flow.Log("tts_muted", map[string]any{"run_id": runIDs[0], "text": text}, runIDs[0])
		return fmt.Errorf("%w: %v", ErrHarnessResultSpeechSuppressed, err)
	}
	return err
}

// DeliverHarnessQuestion exposes a structured question without consuming the result route
// (deduplicated per run and question ID).
func (h *AgentHandler) DeliverHarnessQuestion(runID, questionID, text string) bool {
	if runID == "" || questionID == "" || text == "" {
		return false
	}
	h.harnessRepliesMu.Lock()
	state, ok := h.harnessReplies[runID]
	if !ok || state.delivered || state.localOnly || time.Since(state.created) > 15*time.Minute || state.questionIDs[questionID] || len(state.questionIDs) >= 64 {
		h.harnessRepliesMu.Unlock()
		return false
	}
	if state.questionIDs == nil {
		state.questionIDs = map[string]bool{}
	}
	state.questionIDs[questionID] = true
	h.harnessReplies[runID] = state
	h.harnessRepliesMu.Unlock()
	sensinghttp.DefaultFillerManager.Cancel(runID)
	if h.monitorBus != nil {
		h.monitorBus.Push(domain.MonitorEvent{Type: "assistant_delta", Summary: text, RunID: runID, Detail: map[string]string{"role": "assistant", "source": "harness", "question_id": questionID}})
	}
	if !state.webChat && !state.restored {
		h.deliverTTSUnless(h.isHarnessSpeechCancelled, func(text string) error {
			return hal.AnnounceHarnessUpdate(hal.HarnessUpdateQuestion, text, runID, "")
		}, text, runID, "speak Harness question")
	}
	return true
}
