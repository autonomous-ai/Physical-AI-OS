package http

import (
	"fmt"
	"testing"
	"time"
)

func TestManualCaptureRetainsAgeAcrossDelayedDispatch(t *testing.T) {
	now := time.Now()
	capture := now.Add(-time.Second).UnixMilli()
	req := SensingEventRequest{Type: "voice_command", CapturedAtMS: capture, HarnessVoice: &HarnessVoiceSnapshot{}}
	got := manualCaptureRunID("device-chat-42-1791400000000", req, now)
	if got != fmt.Sprintf("device-chat-42-%d", capture) {
		t.Fatal(got)
	}
	req.HarnessVoice.Enabled = true
	if manualCaptureRunID("original", req, now) != "original" {
		t.Fatal("changed Harness turn")
	}
	req.HarnessVoice = nil
	if manualCaptureRunID("original", req, now) != "original" {
		t.Fatal("changed non-device turn")
	}
}

func TestSupersededFillerCannotRegisterAfterDelayedDispatch(t *testing.T) {
	fm := NewFillerManager()
	cutoff := time.Now().UnixMilli()
	old := fmt.Sprintf("device-chat-1-%d", cutoff-1)
	next := fmt.Sprintf("device-chat-2-%d", cutoff+1)
	fm.CancelBefore(cutoff)
	fm.MarkVoiceRun(old, "old")
	fm.MarkVoiceRun(next, "next")
	if fm.voiceRuns[old] || !fm.voiceRuns[next] {
		t.Fatal("filler eligibility crossed cutoff")
	}
	fm.CancelBefore(cutoff - 100)
	if fm.supersededBefore.Load() != cutoff {
		t.Fatal("stale cancellation regressed")
	}
}
