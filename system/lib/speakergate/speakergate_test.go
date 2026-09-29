package speakergate

import (
	"sync/atomic"
	"testing"
	"time"
)

// withSpeaker swaps the speaker probe and speeds the poll up for the test.
func withSpeaker(t *testing.T, busy func() bool) {
	t.Helper()
	origBusy, origPoll, origWait := SpeakerBusy, pollInterval, maxWait
	SpeakerBusy, pollInterval, maxWait = busy, time.Millisecond, 200*time.Millisecond
	t.Cleanup(func() {
		SpeakerBusy, pollInterval, maxWait = origBusy, origPoll, origWait
		deferring.Store(false)
	})
}

func TestReplayRunsImmediatelyWhenSpeakerIdle(t *testing.T) {
	withSpeaker(t, func() bool { return false })
	if DeferReplay([]string{"presence.enter"}, func() {}) {
		t.Fatal("idle speaker must not defer the replay")
	}
}

func TestPassiveEventWaitsForTheSpeaker(t *testing.T) {
	var busy atomic.Bool
	busy.Store(true)
	withSpeaker(t, busy.Load)

	done := make(chan struct{})
	if !DeferReplay([]string{"presence.enter"}, func() { close(done) }) {
		t.Fatal("presence.enter must be deferred while the device is speaking")
	}
	select {
	case <-done:
		t.Fatal("replay retried while the speaker was still busy")
	case <-time.After(20 * time.Millisecond):
	}

	busy.Store(false)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("replay was never retried after the speaker went idle")
	}
}

// Voice barge-in and fire alerts must replay immediately.
func TestExemptEventsInterruptTheSpeaker(t *testing.T) {
	withSpeaker(t, func() bool { return true })
	for _, evType := range []string{"voice", "voice_command", "voice_followup",
		"voice_agent_handled", "web_chat", "mqtt_chat", "fire_hazard.detected"} {
		if DeferReplay([]string{evType}, func() {}) {
			t.Fatalf("%s must replay immediately, even mid-utterance", evType)
		}
	}
}

// One exempt event releases the whole batch to preserve queue order.
func TestMixedBatchIsNotDeferred(t *testing.T) {
	withSpeaker(t, func() bool { return true })
	if DeferReplay([]string{"presence.enter", "voice"}, func() {}) {
		t.Fatal("a batch containing a voice event must not be deferred")
	}
}

// A stuck `speaking` flag must delay the replay, never cancel it.
func TestReplayProceedsAfterMaxWait(t *testing.T) {
	withSpeaker(t, func() bool { return true })
	done := make(chan struct{})
	if !DeferReplay([]string{"motion.activity"}, func() { close(done) }) {
		t.Fatal("expected deferral")
	}
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("replay never resumed after maxWait")
	}
}

// The waiter must release its slot before retrying so a re-entrant drain can open a fresh one.
func TestReentrantDeferralOpensANewWaiter(t *testing.T) {
	var busy atomic.Bool
	busy.Store(true)
	withSpeaker(t, busy.Load)

	var calls atomic.Int32
	second := make(chan struct{})
	var retry func()
	retry = func() {
		if calls.Add(1) == 1 {
			// Speaker busy again by the time the drain runs.
			busy.Store(true)
			if !DeferReplay([]string{"presence.enter"}, retry) {
				t.Error("re-entrant deferral was refused")
			}
			busy.Store(false)
			return
		}
		close(second)
	}
	if !DeferReplay([]string{"presence.enter"}, retry) {
		t.Fatal("expected first deferral")
	}
	busy.Store(false)
	select {
	case <-second:
	case <-time.After(2 * time.Second):
		t.Fatal("second replay never ran — the waiter slot was not released")
	}
}
