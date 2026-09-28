package hal

import (
	"reflect"
	"strconv"
	"sync"
	"testing"
	"time"
)

func testFollowupTracker() (*followupTracker, *[]followupActivity) {
	var sent []followupActivity
	return &followupTracker{
		runs: make(map[string]*followupRun),
		send: func(activity followupActivity) { sent = append(sent, activity) },
	}, &sent
}

func TestFollowupCancelSendsBatchConcurrentlyAfterRemovingOwners(t *testing.T) {
	const count = 4
	entered := make(chan followupActivity, count)
	release := make(chan struct{})
	var releaseOnce sync.Once
	defer releaseOnce.Do(func() { close(release) })
	tracker := &followupTracker{
		runs: make(map[string]*followupRun),
		send: func(activity followupActivity) {
			if activity.Phase == "cancel" {
				entered <- activity
				<-release
			}
		},
	}
	for i := 0; i < count; i++ {
		id := "run" + strconv.Itoa(i)
		tracker.start("interaction"+strconv.Itoa(i), id)
		tracker.end(id)
	}
	mark := time.Now().UnixMilli()
	done := make(chan struct{})
	go func() {
		tracker.cancel(mark)
		close(done)
	}()
	// No cancellation may wait for an earlier owner's network response.
	for i := 0; i < count; i++ {
		select {
		case <-entered:
		case <-time.After(time.Second):
			t.Fatal("cancellation serialized network requests")
		}
	}
	tracker.mu.Lock()
	remaining := len(tracker.runs)
	tracker.mu.Unlock()
	if remaining != 0 {
		t.Fatalf("cancelled owners remain during network send: %d", remaining)
	}
	// A newer turn must not wait for the cancelled owners' HTTP responses.
	newID := "device-voice-" + strconv.FormatInt(mark+1, 10)
	tracker.start("new-interaction", newID)
	releaseOnce.Do(func() { close(release) })
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("cancellation did not join the completed batch")
	}
	if tracker.runs[newID] == nil {
		t.Fatal("cancel removed newer owner")
	}
}

func TestFollowupTerminalWaitsForAllSpeechAdmissions(t *testing.T) {
	tracker, sent := testFollowupTracker()
	tracker.start("interaction", "run")
	first := tracker.speech("run")
	final := tracker.speech("run")
	tracker.end("run")
	final() // Final admission may return before the streamed first sentence.
	if len(*sent) != 1 {
		t.Fatalf("ended before all submissions returned: %+v", *sent)
	}
	first()
	first() // Cleanup is idempotent, including duplicate terminal delivery.
	tracker.end("run")
	want := []followupActivity{{"interaction", "run", "start"}, {"interaction", "run", "end"}}
	if !reflect.DeepEqual(*sent, want) {
		t.Fatalf("notifications = %+v, want %+v", *sent, want)
	}
}

func TestFollowupNoReplyAndDispatchFailureEndWithoutSpeech(t *testing.T) {
	tracker, sent := testFollowupTracker()
	tracker.start("interaction", "run")
	tracker.end("run")
	if len(*sent) != 2 || (*sent)[1].Phase != "end" || !tracker.runs["run"].endSent {
		t.Fatalf("silent terminal leaked processing: %+v", *sent)
	}
}

func TestFollowupCancelAfterProcessingEndsStillCancelsPlayback(t *testing.T) {
	tracker, sent := testFollowupTracker()
	tracker.start("interaction", "run")
	admitted := tracker.speech("run")
	tracker.end("run")
	admitted() // HAL accepted audio; its physical playback is still pending.
	tracker.end("run")
	tracker.cancel(time.Now().UnixMilli())
	admitted()
	tracker.end("run")
	want := []followupActivity{
		{"interaction", "run", "start"},
		{"interaction", "run", "end"},
		{"interaction", "run", "cancel"},
	}
	if !reflect.DeepEqual(*sent, want) || len(tracker.runs) != 0 {
		t.Fatalf("playback cancellation = %+v, want %+v", *sent, want)
	}
}

func TestFollowupCompletedCancellationMetadataExpires(t *testing.T) {
	tracker, sent := testFollowupTracker()
	tracker.start("interaction", "run")
	tracker.end("run")
	tracker.runs["run"].started = time.Now().Add(-followupActivityTTL - time.Second)
	tracker.cancel(time.Now().UnixMilli())
	if len(*sent) != 2 || len(tracker.runs) != 0 {
		t.Fatalf("expired completion retained or sent cancel: %+v", *sent)
	}
}

func TestFollowupCancellationCannotBeUndoneByLateAdmission(t *testing.T) {
	tracker, sent := testFollowupTracker()
	tracker.start("older", "old-run")
	done := tracker.speech("old-run")
	mark := time.Now().UnixMilli()
	tracker.cancel(mark)
	newID := "device-voice-" + strconv.FormatInt(mark+1, 10)
	tracker.start("newer", newID)
	done()
	tracker.end("old-run")
	tracker.end(newID)
	want := []followupActivity{
		{"older", "old-run", "start"}, {"older", "old-run", "cancel"},
		{"newer", newID, "start"}, {"newer", newID, "end"},
	}
	if !reflect.DeepEqual(*sent, want) {
		t.Fatalf("notifications = %+v, want %+v", *sent, want)
	}
}

func TestFollowupCancelPreservesNewerRunAndRejectsDelayedOldStart(t *testing.T) {
	tracker, sent := testFollowupTracker()
	mark := time.Now().UnixMilli()
	oldID := "device-voice-" + strconv.FormatInt(mark-1, 10)
	newID := "device-voice-" + strconv.FormatInt(mark+1, 10)
	tracker.start("newer", newID)
	tracker.cancel(mark)
	tracker.start("older", oldID)
	tracker.end(newID)
	want := []followupActivity{{"newer", newID, "start"}, {"newer", newID, "end"}}
	if !reflect.DeepEqual(*sent, want) {
		t.Fatalf("cancel/start race notifications = %+v, want %+v", *sent, want)
	}
}

func TestFollowupIrrelevantRunsDoNotContactHAL(t *testing.T) {
	tracker, sent := testFollowupTracker()
	tracker.start("", "ambient")
	tracker.speech("ambient")()
	tracker.end("ambient")
	tracker.cancel(time.Now().UnixMilli())
	if len(*sent) != 0 {
		t.Fatalf("unregistered run contacted HAL: %+v", *sent)
	}
}

func TestFollowupExpiredRunCannotReleaseNewFocus(t *testing.T) {
	tracker, sent := testFollowupTracker()
	tracker.start("interaction", "run")
	done := tracker.speech("run")
	tracker.end("run")
	tracker.runs["run"].started = time.Now().Add(-followupActivityTTL - time.Second)
	done()
	if len(*sent) != 1 || len(tracker.runs) != 0 {
		t.Fatalf("expired run retained or emitted terminal: %+v", *sent)
	}
}
