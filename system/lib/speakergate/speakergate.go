// Package speakergate defers replay of buffered sensing events until the speaker is idle,
// so a passive event does not start a new turn that cuts off the current reply.
package speakergate

import (
	"log/slog"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/lib/safego"
)

// pollInterval is how often the speaker is re-checked while a replay waits.
var pollInterval = 500 * time.Millisecond

// SpeakerBusy reports whether the device is still speaking (a variable so tests can swap it).
var SpeakerBusy = hal.SpeakerBusy

// maxWait caps the deferral; a stuck speaking flag delays replay, never cancels it.
var maxWait = 90 * time.Second

// deferring ensures at most one waiter polls at a time.
var deferring atomic.Bool

// WaitsForSpeaker reports whether replaying eventType must wait for the speaker to go idle.
// User speech/chat (barge-in) and fire_hazard.detected are exempt.
func WaitsForSpeaker(eventType string) bool {
	switch eventType {
	case "voice", "voice_command", "voice_followup", "voice_agent_handled",
		"web_chat", "mqtt_chat", "fire_hazard.detected":
		return false
	default:
		return true
	}
}

// DeferReplay reports whether a drain must wait for the speaker; if true, the caller re-queues and
// retry runs once the speaker is idle (or after maxWait). One exempt event releases the whole batch.
func DeferReplay(eventTypes []string, retry func()) bool {
	if !SpeakerBusy() {
		return false
	}
	for _, t := range eventTypes {
		if !WaitsForSpeaker(t) {
			return false
		}
	}
	if !deferring.CompareAndSwap(false, true) {
		// Another waiter is already polling; it will drain this batch too.
		return true
	}
	slog.Info("sensing replay deferred -- device still speaking",
		"component", "sensing", "events", len(eventTypes))
	safego.Go("speakergate", func() {
		// Release the flag BEFORE retry (not in a defer) so a re-entrant drain can open a fresh waiter.
		defer deferring.Store(false)
		deadline := time.Now().Add(maxWait)
		for time.Now().Before(deadline) {
			time.Sleep(pollInterval)
			if !SpeakerBusy() {
				slog.Info("sensing replay resumed -- speaker idle", "component", "sensing")
				deferring.Store(false)
				retry()
				return
			}
		}
		slog.Warn("sensing replay resumed -- speaker still busy after max wait",
			"component", "sensing", "max_wait_s", int(maxWait.Seconds()))
		deferring.Store(false)
		retry()
	})
	return true
}
