package http

import (
	"testing"
	"time"
)

// A cancelled turn must not keep re-arming fillers at tool boundaries.
func TestCancelAllActiveEndsEveryInFlightRun(t *testing.T) {
	fm := NewFillerManager()
	fm.MarkVoiceRun("device-chat-54-1787885628360", "")
	fm.OnTurnStart("device-chat-54-1787885628360")
	fm.MarkVoiceRun("device-chat-55-1787885629999", "")
	fm.OnTurnStart("device-chat-55-1787885629999")

	if n := fm.CancelAllActive(); n != 2 {
		t.Fatalf("expected both in-flight runs cancelled, got %d", n)
	}

	fm.mu.Lock()
	remaining := len(fm.runs)
	fm.mu.Unlock()
	if remaining != 0 {
		t.Errorf("runs map should be empty after cancel-all, got %d", remaining)
	}
}

// A cancelled run stays dead as tool boundaries keep arriving.
func TestToolEndAfterCancelAllDoesNotRearm(t *testing.T) {
	fm := NewFillerManager()
	runID := "device-chat-54-1787885628360"
	fm.MarkVoiceRun(runID, "")
	fm.OnTurnStart(runID)
	fm.CancelAllActive()

	fm.OnToolEnd(runID)

	fm.mu.Lock()
	_, revived := fm.runs[runID]
	fm.mu.Unlock()
	if revived {
		t.Errorf("tool.end on a cancelled run must not re-register it for fillers")
	}
}

// A run registered after the click is unaffected.
func TestRunStartedAfterCancelAllStillArms(t *testing.T) {
	fm := NewFillerManager()
	fm.MarkVoiceRun("device-chat-54-1787885628360", "")
	fm.OnTurnStart("device-chat-54-1787885628360")
	fm.CancelAllActive()

	fresh := "device-chat-55-1787885629999"
	fm.MarkVoiceRun(fresh, "")
	fm.OnTurnStart(fresh)

	fm.mu.Lock()
	run, ok := fm.runs[fresh]
	ended := ok && run.ended
	fm.mu.Unlock()
	if !ok {
		t.Fatalf("a turn started after the click must register for fillers")
	}
	if ended {
		t.Errorf("a turn started after the click must not inherit the cancelled state")
	}
}

func TestCancelAllActiveOnIdleManagerIsANoop(t *testing.T) {
	fm := NewFillerManager()
	if n := fm.CancelAllActive(); n != 0 {
		t.Errorf("expected 0 cancelled on an idle manager, got %d", n)
	}
}

// Guards the collect-then-cancel locking split in CancelAllActive.
func TestCancelAllActiveIsSafeAlongsideConcurrentToolEnds(t *testing.T) {
	fm := NewFillerManager()
	for _, runID := range []string{"run-a", "run-b", "run-c"} {
		fm.MarkVoiceRun(runID, "")
		fm.OnTurnStart(runID)
	}

	done := make(chan struct{})
	go func() {
		defer close(done)
		for i := 0; i < 200; i++ {
			fm.OnToolEnd("run-b")
		}
	}()
	fm.CancelAllActive()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("concurrent OnToolEnd deadlocked with CancelAllActive")
	}
}

// The opening filler is owned by HAL's interaction id, not the run id.
func TestFillerOwnerPrefersTheInteractionID(t *testing.T) {
	if got := fillerOwner("vi-abc123", "device-chat-7-1788422075499"); got != "vi-abc123" {
		t.Errorf("fillerOwner = %q, want the interaction id", got)
	}
	if got := fillerOwner("", "device-chat-7-1788422075499"); got != "device-chat-7-1788422075499" {
		t.Errorf("fillerOwner = %q, want the run id fallback", got)
	}
	if got := fillerOwner("", ""); got != "" {
		t.Errorf("fillerOwner = %q, want empty (unattributed)", got)
	}
}

// Later fillers in a turn carry the same owner as the opening one.
func TestLaterFillersReuseTheTurnsInteractionID(t *testing.T) {
	fm := NewFillerManager()
	fm.MarkVoiceRun("device-chat-7-1788422075499", "vi-abc123")

	fm.mu.Lock()
	got := fillerOwner(fm.interactions["device-chat-7-1788422075499"], "device-chat-7-1788422075499")
	fm.mu.Unlock()

	if got != "vi-abc123" {
		t.Errorf("owner = %q, want the interaction id recorded at MarkVoiceRun", got)
	}
}

// Forgetting a run must forget its interaction mapping too.
func TestCancelClearsTheInteractionMapping(t *testing.T) {
	fm := NewFillerManager()
	fm.MarkVoiceRun("device-chat-7-1788422075499", "vi-abc123")
	fm.Cancel("device-chat-7-1788422075499")

	fm.mu.Lock()
	_, ok := fm.interactions["device-chat-7-1788422075499"]
	fm.mu.Unlock()
	if ok {
		t.Error("interaction mapping leaked after Cancel")
	}
}
