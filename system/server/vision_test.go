package server

import (
	"testing"
	"time"
)

func stubSpeaker(t *testing.T, busy func(n int) bool) *int {
	t.Helper()
	calls := 0
	prevBusy, prevPoll := cueSpeakerBusy, cuePoll
	cueSpeakerBusy = func() bool { calls++; return busy(calls) }
	cuePoll = time.Millisecond
	t.Cleanup(func() { cueSpeakerBusy, cuePoll = prevBusy, prevPoll })
	return &calls
}

func TestWaitForCueReturnsOnceTheCueHasPlayed(t *testing.T) {
	// Silent, then speaking for three polls, then done.
	calls := stubSpeaker(t, func(n int) bool { return n >= 2 && n <= 4 })
	start := time.Now()
	waitForCue(time.Second, 500*time.Millisecond)
	if *calls != 5 {
		t.Fatalf("should stop at the first silent poll after speech: %d polls", *calls)
	}
	if time.Since(start) > 200*time.Millisecond {
		t.Fatal("finished cue should not wait out the cap")
	}
}

func TestWaitForCueGivesUpWhenSpeechNeverStarts(t *testing.T) {
	stubSpeaker(t, func(int) bool { return false })
	start := time.Now()
	waitForCue(time.Second, 20*time.Millisecond)
	if d := time.Since(start); d < 20*time.Millisecond || d > 200*time.Millisecond {
		t.Fatalf("a cue that never starts should cost only the start window, took %v", d)
	}
}

func TestWaitForCueIsBoundedBySpeechThatNeverEnds(t *testing.T) {
	stubSpeaker(t, func(int) bool { return true })
	start := time.Now()
	waitForCue(30*time.Millisecond, 10*time.Millisecond)
	if d := time.Since(start); d < 30*time.Millisecond || d > 250*time.Millisecond {
		t.Fatalf("a stuck speaker must not hold the photo past the cap, took %v", d)
	}
}
