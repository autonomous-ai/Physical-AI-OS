package http

import (
	"strings"
	"testing"
)

func stubCueSpeech(t *testing.T) *[]string {
	t.Helper()
	var spoken []string
	prev := speakCue
	speakCue = func(text, owner string) error {
		spoken = append(spoken, text+"|"+owner)
		return nil
	}
	t.Cleanup(func() { speakCue = prev })
	return &spoken
}

func TestSayInVoiceRunIsSilentWithoutAVoiceTurn(t *testing.T) {
	spoken := stubCueSpeech(t)
	fm := NewFillerManager()
	// A chat turn is never marked as voice, so it holds no filler state.
	fm.OnTurnStart("telegram-turn")
	if fm.SayInVoiceRun("look_analyzing") {
		t.Fatal("a cue must not play when no voice turn is running")
	}
	if len(*spoken) != 0 {
		t.Fatalf("spoke %v outside a voice turn", *spoken)
	}
}

func TestSayInVoiceRunSpeaksAndPushesBackTheDeadAirFiller(t *testing.T) {
	spoken := stubCueSpeech(t)
	fm, id, run := startFillerTestRun(t)
	fm.OnToolStart(id, "", "terminal")
	if run.timer == nil {
		t.Fatal("precondition: the tool start should arm a dead-air filler")
	}
	before := run.lastActivityAt

	if !fm.SayInVoiceRun("look_analyzing") {
		t.Fatal("a cue must play during a voice turn")
	}
	want := map[string]bool{"Got it — give me a sec.|" + id: true, "Okay, thinking about it.|" + id: true}
	if len(*spoken) != 1 || !want[(*spoken)[0]] {
		t.Fatalf("unexpected cue %v", *spoken)
	}
	if !run.lastActivityAt.After(before) {
		t.Fatal("the cue must count as activity so the generic filler waits its cooldown")
	}
	if run.timer == nil {
		t.Fatal("the dead-air filler must be re-armed, not dropped, for the long wait that follows")
	}
	if run.fired != 1 {
		t.Fatalf("a cue must not use up the per-turn filler cap: fired=%d", run.fired)
	}
}

func TestSayInVoiceRunSkipsASuspendedTurn(t *testing.T) {
	spoken := stubCueSpeech(t)
	fm, id, _ := startFillerTestRun(t)
	fm.OnAssistantText(id)
	if fm.SayInVoiceRun("look_capturing_main") {
		t.Fatal("a cue must not talk over the reply that is already streaming")
	}
	if len(*spoken) != 0 {
		t.Fatalf("spoke %v over a streaming reply", *spoken)
	}
}

func TestSayInVoiceRunIgnoresAnUnknownPool(t *testing.T) {
	spoken := stubCueSpeech(t)
	fm, _, _ := startFillerTestRun(t)
	if fm.SayInVoiceRun("no_such_pool") {
		t.Fatal("an unknown pool has nothing to say")
	}
	if len(*spoken) != 0 {
		t.Fatalf("spoke %v for an unknown pool", *spoken)
	}
}

func startSuppressedTurn(t *testing.T) (*FillerManager, string) {
	t.Helper()
	fm := NewFillerManager()
	id := "silent-followup-look"
	fm.SuppressRun(id)
	fm.OnTurnStart(id)
	t.Cleanup(func() { fm.Cancel(id) })
	return fm, id
}

func TestSuppressedFollowUpSpeaksCuesButNoFiller(t *testing.T) {
	spoken := stubCueSpeech(t)
	fm, id := startSuppressedTurn(t)
	fm.OnToolStart(id, `{}`, "terminal")
	fm.OnToolEnd(id)
	if fm.HasActiveRun(id) {
		t.Fatal("a suppressed follow-up must not hold automatic-filler state")
	}
	if !fm.SayInVoiceRun("look_capturing_main") {
		t.Fatal("an action cue must play on a suppressed follow-up")
	}
	if len(*spoken) != 1 || !strings.HasSuffix((*spoken)[0], "|"+id) {
		t.Fatalf("unexpected cue %v", *spoken)
	}
	if fm.HasActiveRun(id) {
		t.Fatal("a cue must not arm automatic fillers on a suppressed follow-up")
	}
}

func TestSuppressedFollowUpCueWaitsOutAStreamingReply(t *testing.T) {
	spoken := stubCueSpeech(t)
	fm, id := startSuppressedTurn(t)
	fm.OnAssistantText(id)
	if fm.SayInVoiceRun("look_analyzing") {
		t.Fatal("a cue must not talk over the reply that is already streaming")
	}
	fm.OnToolStart(id, `{}`, "terminal")
	if !fm.SayInVoiceRun("look_analyzing") || len(*spoken) != 1 {
		t.Fatalf("a new tool call resumes cues: %v", *spoken)
	}
}

func TestCancelEndsTheSuppressedFollowUpsCues(t *testing.T) {
	stubCueSpeech(t)
	fm, id := startSuppressedTurn(t)
	fm.Cancel(id)
	if fm.SayInVoiceRun("look_capturing_main") {
		t.Fatal("a finished turn has no cue to speak")
	}
}
