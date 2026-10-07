package http

import "testing"

func startFillerTestRun(t *testing.T) (*FillerManager, string, *fillerRun) {
	t.Helper()
	fm := NewFillerManager()
	id := "voice-tool-turn"
	fm.MarkVoiceRun(id, "")
	fm.OnTurnStart(id)
	t.Cleanup(func() { fm.Cancel(id) })
	return fm, id, fm.runs[id]
}

func TestAssistantTextSuspendsUntilNextToolStart(t *testing.T) {
	fm, id, run := startFillerTestRun(t)
	fm.OnToolStart(id, "", "search_files")
	fm.OnAssistantText(id)
	if !run.suspended || run.timer != nil || !fm.HasActiveRun(id) {
		t.Fatal("assistant text must stop the timer but retain the voice run")
	}
	fm.OnToolEnd(id)
	if run.timer != nil || run.rearmPending {
		t.Fatal("a late tool result must not resume suspended fillers")
	}
	fm.OnToolStart(id, "", "terminal")
	if run.suspended || run.timer == nil || run.lastToolName != "terminal" {
		t.Fatal("the next tool must resume fillers and record its name")
	}
	if run.fired != 1 {
		t.Fatalf("suspension changed the existing filler count: %d", run.fired)
	}
	fm.OnAssistantText(id)
	if run.timer != nil || !run.suspended {
		t.Fatal("new assistant text must suspend the resumed timer")
	}
}

func TestSuspendedFillerCannotReviveAfterCancellation(t *testing.T) {
	for _, cancelAll := range []bool{false, true} {
		fm, id, _ := startFillerTestRun(t)
		fm.OnAssistantText(id)
		if cancelAll {
			fm.CancelAllActive()
		} else {
			fm.Cancel(id)
		}
		fm.OnToolStart(id, "", "terminal")
		fm.OnToolEnd(id)
		fm.OnTurnStart(id)
		if fm.HasActiveRun(id) {
			t.Fatal("terminal cancellation must prevent later tool events reviving the run")
		}
	}
}

func TestSuspendedFillerPreservesCapAndHardwareSuppression(t *testing.T) {
	fm, id, run := startFillerTestRun(t)
	fm.OnAssistantText(id)
	run.fired = MaxFillersPerTurn
	fm.OnToolStart(id, "", "terminal")
	if run.timer != nil {
		t.Fatal("resuming a tool must not bypass the per-turn cap")
	}
	run.fired = 1
	fm.OnAssistantText(id)
	fm.OnToolStart(id, "curl /emotion", "terminal")
	if run.timer != nil {
		t.Fatal("a hardware reaction must not resume an audible filler")
	}
}

func TestSuspensionInvalidatesOldTimerAndSpeechCompletion(t *testing.T) {
	fm, id, run := startFillerTestRun(t)
	generation := run.generation
	run.rearmPending = true
	fm.OnAssistantText(id)
	if run.rearmPending {
		t.Fatal("suspension must discard deferred tool-end rearming")
	}
	fm.finishFiller(id, run, generation, "old filler")
	if run.timer != nil || run.lastSpoken != "" {
		t.Fatal("old speech completion mutated the suspended run")
	}
	fm.OnToolStart(id, "", "skill_view")
	timer := run.timer
	fm.fire(id, run, generation)
	fm.finishFiller(id, run, generation, "old filler")
	if run.timer != timer || run.lastSpoken != "" || run.fired != 1 {
		t.Fatal("old timer or speech completion mutated the resumed run")
	}
}

func TestAssistantAndToolEventsDoNotCreateUnmarkedVoiceRuns(t *testing.T) {
	fm := NewFillerManager()
	fm.OnAssistantText("web-chat")
	fm.OnToolStart("web-chat", "", "terminal")
	fm.OnToolEnd("web-chat")
	if fm.HasActiveRun("web-chat") {
		t.Fatal("web chat must remain ineligible for spoken fillers")
	}
}

func TestOneContinuationPreventsLaterToolFillers(t *testing.T) {
	fm, id, run := startFillerTestRun(t)
	fm.OnAssistantText(id)
	// One continuation has consumed the slot after the reserved opening.
	run.fired++
	fm.OnToolStart(id, "", "terminal")
	fm.OnToolEnd(id)
	if run.timer != nil || run.rearmPending {
		t.Fatal("later tool events must not schedule a second continuation")
	}
}
