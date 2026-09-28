package http

import (
	"testing"
	"time"

	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
)

// A main-agent turn on an older question must not speak after the realtime answer.
func TestRealtimeHandledMutesOlderInFlightTurn(t *testing.T) {
	t.Setenv("OS_REALTIME_SUPERSEDES_MAIN_REPLY", "1")
	h := newCancelTestHandler()
	older := deviceRunID(5, time.Now().Add(-2*time.Second))

	h.CancelSpeechForNewerTurn()

	if !h.isSpeechCancelled(older) {
		t.Fatalf("turn in flight when realtime answered a newer utterance must lose the speaker")
	}
}

// The system-stamped mark mutes the speaker only; the turn's body still runs.
func TestRealtimeHandledDoesNotDropHardware(t *testing.T) {
	t.Setenv("OS_REALTIME_SUPERSEDES_MAIN_REPLY", "1")
	h := newCancelTestHandler()
	older := deviceRunID(5, time.Now().Add(-2*time.Second))

	h.CancelSpeechForNewerTurn()

	if h.isHWCancelled(older) {
		t.Errorf("auto mute must not drop servo/LED markers — only the physical click goes that far")
	}
}

// Fillers follow the speech: the auto mark disarms them too.
func TestRealtimeHandledDropsPendingFillers(t *testing.T) {
	t.Setenv("OS_REALTIME_SUPERSEDES_MAIN_REPLY", "1")
	h := newCancelTestHandler()
	fm := sensinghttp.DefaultFillerManager
	runID := "device-chat-54-1787885628360"
	fm.MarkVoiceRun(runID, "")
	fm.OnTurnStart(runID)
	t.Cleanup(func() { fm.Cancel(runID) })

	h.CancelSpeechForNewerTurn()

	fm.OnToolEnd(runID)
	if fm.HasActiveRun(runID) {
		t.Errorf("the muted turn must not keep re-arming fillers for an answer it can no longer speak")
	}
}

// ...but the switch still governs it: not opted in means nothing changes.
func TestSupersedeOffLeavesFillersAlone(t *testing.T) {
	h := newCancelTestHandler()
	fm := sensinghttp.DefaultFillerManager
	runID := "device-chat-55-1787885629999"
	fm.MarkVoiceRun(runID, "")
	fm.OnTurnStart(runID)
	t.Cleanup(func() { fm.Cancel(runID) })

	h.CancelSpeechForNewerTurn()

	if !fm.HasActiveRun(runID) {
		t.Errorf("without OS_REALTIME_SUPERSEDES_MAIN_REPLY=1 filler state must be untouched")
	}
}

// The physical click keeps its stronger meaning now that a second mark exists.
func TestPhysicalClickStillDropsHardware(t *testing.T) {
	h := newCancelTestHandler()
	older := deviceRunID(5, time.Now().Add(-2*time.Second))

	h.CancelSpeech()

	if !h.isHWCancelled(older) {
		t.Errorf("click must still drop the body of an in-flight turn")
	}
}

// A turn started after the mark still speaks, which is why the mark is a timestamp.
func TestTurnStartedAfterRealtimeHandledStillSpeaks(t *testing.T) {
	t.Setenv("OS_REALTIME_SUPERSEDES_MAIN_REPLY", "1")
	h := newCancelTestHandler()

	h.CancelSpeechForNewerTurn()

	newer := deviceRunID(6, time.Now().Add(2*time.Second))
	if h.isSpeechCancelled(newer) {
		t.Errorf("a turn created after the realtime answer must speak")
	}
}

// The auto and click marks are independent and do not widen each other.
func TestMarksDoNotOverwriteEachOther(t *testing.T) {
	t.Setenv("OS_REALTIME_SUPERSEDES_MAIN_REPLY", "1")
	h := newCancelTestHandler()
	h.CancelSpeech()
	// Strictly between the two marks: after the click, before the realtime answer.
	time.Sleep(2 * time.Millisecond)
	betweenTurn := deviceRunID(7, time.Now())
	time.Sleep(2 * time.Millisecond)
	h.CancelSpeechForNewerTurn()

	if !h.isSpeechCancelled(betweenTurn) {
		t.Errorf("turn created after the click but before the realtime answer must be muted by the auto mark")
	}
	if h.isHWCancelled(betweenTurn) {
		t.Errorf("that turn started after the click, so its body must still run")
	}
}

// Off by default: the older turn still speaks after a realtime answer.
func TestSupersedeIsOffUnlessOptedIn(t *testing.T) {
	h := newCancelTestHandler()
	older := deviceRunID(5, time.Now().Add(-2*time.Second))

	h.CancelSpeechForNewerTurn()

	if h.isSpeechCancelled(older) {
		t.Errorf("without OS_REALTIME_SUPERSEDES_MAIN_REPLY=1 the in-flight turn must stay audible")
	}
}
