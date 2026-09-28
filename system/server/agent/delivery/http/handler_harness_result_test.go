package http

import (
	"errors"
	"testing"
	"time"

	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/monitor"
)

func TestHarnessGroupedResultPublishesOneAnswerAndReferences(t *testing.T) {
	bus := monitor.ProvideBus()
	events, unsub := bus.Subscribe()
	defer unsub()
	h := &AgentHandler{monitorBus: bus}
	h.MarkHarnessResponseRun("a", true, false)
	h.MarkHarnessResponseRun("b", true, false)
	if !h.DeliverHarnessGroupedResult("result-1", "completed", "House with trees", []string{"a", "b"}) {
		t.Fatal("not delivered")
	}
	first, second := <-events, <-events
	if first.Summary != "House with trees" || first.RunID != "b" || second.Summary == first.Summary || second.RunID != "a" {
		t.Fatalf("wrong group output: %+v %+v", first, second)
	}
	if h.DeliverHarnessGroupedResult("result-1", "completed", "House with trees", []string{"a", "b"}) {
		t.Fatal("duplicate delivered")
	}
	if h.DeliverHarnessResponse("b", "late legacy summary") {
		t.Fatal("legacy response escaped tombstone")
	}
}

func TestHarnessGroupedResultRejectsMembershipAtomically(t *testing.T) {
	for _, ids := range [][]string{{"a", "missing"}, {"a", "a"}, {"a", "local"}} {
		h := &AgentHandler{}
		h.MarkHarnessResponseRun("a", true, false)
		h.MarkHarnessLocalResponseRun("local", true)
		if h.DeliverHarnessGroupedResult("r", "completed", "Result", ids) {
			t.Fatal("invalid membership accepted")
		}
		if h.harnessReplies["a"].delivered {
			t.Fatal("partially closed group")
		}
	}
}

func TestHarnessGroupedSpeechSingleSynchronousSubmission(t *testing.T) {
	h := &AgentHandler{}
	h.MarkHarnessResponseRun("a", false, false)
	h.MarkHarnessResponseRun("b", false, false)
	calls := 0
	send := func(text, owner string) error {
		calls++
		if text != "Result" || owner != "b" {
			t.Fatalf("wrong submission %q %q", text, owner)
		}
		return nil
	}
	if err := h.speakHarnessGroupedResult("Result", []string{"a", "b"}, send); err != nil {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatalf("calls %d", calls)
	}
}

func TestHarnessGroupedSpeechRejectsAnyIneligibleMember(t *testing.T) {
	for _, reason := range []string{"web", "local", "expired", "missing", "cancelled", "restored"} {
		t.Run(reason, func(t *testing.T) {
			h := &AgentHandler{}
			h.MarkHarnessResponseRun("a", false, false)
			h.MarkHarnessResponseRun("b", false, false)
			state := h.harnessReplies["b"]
			switch reason {
			case "web":
				state.webChat = true
			case "local":
				state.localOnly = true
			case "restored":
				state.restored = true
			case "expired":
				state.created = time.Now().Add(-16 * time.Minute)
			}
			h.harnessReplies["b"] = state
			if reason == "missing" {
				delete(h.harnessReplies, "b")
			}
			if reason == "cancelled" {
				h.speechWatermarkMs.Store(time.Now().UnixMilli() + 1)
			}
			err := h.speakHarnessGroupedResult("Result", []string{"a", "b"}, func(string, string) error { t.Fatal("submitted suppressed result"); return nil })
			if !errors.Is(err, ErrHarnessResultSpeechSuppressed) {
				t.Fatalf("wrong error %v", err)
			}
		})
	}
}

func TestHarnessGroupedSpeechPreservesUnknownAndMutedOutcomes(t *testing.T) {
	for _, sendErr := range []error{errors.New("lost HAL acknowledgement"), hal.ErrSpeakerMuted} {
		h := &AgentHandler{}
		h.MarkHarnessResponseRun("a", false, false)
		calls := 0
		err := h.speakHarnessGroupedResult("Result", []string{"a"}, func(string, string) error { calls++; return sendErr })
		if calls != 1 {
			t.Fatal("retried request")
		}
		if errors.Is(sendErr, hal.ErrSpeakerMuted) {
			if !errors.Is(err, ErrHarnessResultSpeechSuppressed) {
				t.Fatal(err)
			}
		} else if err != sendErr {
			t.Fatal("lost uncertainty", err)
		}
	}
}

func TestHarnessQuestionKeepsResultPendingAndDeduplicatesReplay(t *testing.T) {
	bus := monitor.ProvideBus()
	events, unsub := bus.Subscribe()
	defer unsub()
	h := &AgentHandler{monitorBus: bus}
	h.MarkHarnessResponseRun("a", true, false)
	for _, id := range []string{"q1", "q2"} {
		if !h.DeliverHarnessQuestion("a", id, "Choose a color") {
			t.Fatal("question missing")
		}
		event := <-events
		if event.Type != "assistant_delta" || event.Summary != "Choose a color" {
			t.Fatalf("wrong event %+v", event)
		}
	}
	if h.DeliverHarnessQuestion("a", "q1", "replay") {
		t.Fatal("replayed old question")
	}
	if h.harnessReplies["a"].delivered || h.harnessReplies["a"].localOnly {
		t.Fatal("question consumed remote result")
	}
	if !h.DeliverHarnessGroupedResult("r", "completed", "Final answer", []string{"a"}) {
		t.Fatal("question prevented result")
	}
	if h.DeliverHarnessQuestion("a", "q3", "late") {
		t.Fatal("question reopened completed result")
	}
}

func TestHarnessQuestionRejectsUnavailableRoutes(t *testing.T) {
	h := &AgentHandler{}
	h.MarkHarnessLocalResponseRun("local", true)
	h.MarkHarnessResponseRun("expired", true, false)
	state := h.harnessReplies["expired"]
	state.created = time.Now().Add(-16 * time.Minute)
	h.harnessReplies["expired"] = state
	for _, id := range []string{"", "unknown", "local", "expired"} {
		if h.DeliverHarnessQuestion(id, "q", "Question") {
			t.Fatal("invalid route", id)
		}
	}
}

func TestHarnessGroupedUsageLimitNeverRewritesResult(t *testing.T) {
	h := &AgentHandler{}
	h.MarkHarnessResponseRun("a", false, false)
	err := h.speakHarnessGroupedResult("You've reached the usage limit", []string{"a"}, func(string, string) error { t.Fatal("rewritten/submitted banner"); return nil })
	if !errors.Is(err, ErrHarnessResultSpeechSuppressed) {
		t.Fatal(err)
	}
}

// Reproduces the device report: A begins, speech is cancelled, then B is
// submitted. A/B complete together. The new request must still own its reply.
func TestHarnessGroupedSpeechNewInputAfterCancelOwnsSharedReply(t *testing.T) {
	old := "device-chat-1-1790308528212"
	latest := "device-chat-2-1790308585451"
	for _, order := range [][]string{{old, latest}, {latest, old}} {
		for _, source := range []string{"click", "realtime"} {
			t.Run(source+"/"+order[0], func(t *testing.T) {
				h := &AgentHandler{}
				// Registration order is deliberately reversed: replay/callback
				// arrival must not decide which device request is newest.
				h.MarkHarnessResponseRun(latest, false, false)
				h.MarkHarnessResponseRun(old, false, false)
				if source == "click" {
					h.speechWatermarkMs.Store(1790308578529)
				} else {
					h.autoSpeechWatermarkMs.Store(1790308578529)
				}
				if !h.isSpeechCancelled(old) || h.isSpeechCancelled(latest) {
					t.Fatal("invalid reproduction setup")
				}
				calls := 0
				originalFirst := order[0]
				err := h.speakHarnessGroupedResult("All clouds are black.", order, func(text, owner string) error {
					calls++
					if owner != latest || text != "All clouds are black." {
						t.Fatalf("wrong speech owner/content: %s %s", owner, text)
					}
					return nil
				})
				if err != nil || calls != 1 {
					t.Fatalf("new input lost its shared reply: %v calls=%d", err, calls)
				}
				if order[0] != originalFirst {
					t.Fatal("mutated caller membership")
				}
			})
		}
	}
}

func TestHarnessGroupedSpeechStillHonorsCancelAfterLatestInput(t *testing.T) {
	h := &AgentHandler{}
	ids := []string{"device-chat-1-1790308528212", "device-chat-2-1790308585451"}
	for _, id := range ids {
		h.MarkHarnessResponseRun(id, false, false)
	}
	h.speechWatermarkMs.Store(1790308585452)
	err := h.speakHarnessGroupedResult("All clouds are black.", ids, func(string, string) error { t.Fatal("spoke after user cancelled latest request"); return nil })
	if !errors.Is(err, ErrHarnessResultSpeechSuppressed) {
		t.Fatalf("wrong cancellation %v", err)
	}
}

func TestHarnessGroupedResultShowsFullAnswerOnNewestDeviceInput(t *testing.T) {
	bus := monitor.ProvideBus()
	events, unsub := bus.Subscribe()
	defer unsub()
	h := &AgentHandler{monitorBus: bus}
	old, latest := "device-chat-1-1790308528212", "device-chat-2-1790308585451"
	h.MarkHarnessResponseRun(latest, true, false)
	h.MarkHarnessResponseRun(old, true, false)
	if !h.DeliverHarnessGroupedResult("shared", "completed", "All clouds are black.", []string{old, latest}) {
		t.Fatal("not delivered")
	}
	first, second := <-events, <-events
	if first.RunID != latest || first.Summary != "All clouds are black." || second.RunID != old || second.Detail.(map[string]string)["result_run_id"] != latest || second.Detail.(map[string]string)["result_reference"] != "true" {
		t.Fatalf("incorrect shared reply placement: %+v %+v", first, second)
	}
}
