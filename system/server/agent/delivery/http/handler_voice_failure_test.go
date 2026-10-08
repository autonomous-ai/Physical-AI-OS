package http

import (
	"testing"
	"time"

	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
)

// A spoken request that fails is answered with a short notice, once per window;
// runs HAL never marked as voice stay silent.
func TestVoiceTurnFailureIsSpokenOncePerWindow(t *testing.T) {
	h := newCancelTestHandler()
	spoken := make(chan string, 4)
	// A fresh filler manager: earlier tests leave supersede cutoffs on the shared one.
	origFM := sensinghttp.DefaultFillerManager
	sensinghttp.DefaultFillerManager = sensinghttp.NewFillerManager()
	defer func() { sensinghttp.DefaultFillerManager = origFM }()
	orig := speakVoiceFailure
	speakVoiceFailure = func(text string) error { spoken <- text; return nil }
	defer func() { speakVoiceFailure = orig }()

	chat := deviceRunID(8, time.Now().Add(-time.Second))
	if h.speakVoiceTurnFailure(chat) {
		t.Fatal("a run HAL never marked as voice must stay silent")
	}
	voice := deviceRunID(7, time.Now().Add(-time.Second))
	sensinghttp.DefaultFillerManager.MarkVoiceRun(voice, "vi-7")
	defer sensinghttp.DefaultFillerManager.Cancel(voice)
	if !h.speakVoiceTurnFailure(voice) {
		t.Fatal("a failed voice run should be spoken")
	}
	select {
	case text := <-spoken:
		if text == "" {
			t.Fatal("empty failure phrase")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("failure notice never reached the speaker")
	}
	if h.speakVoiceTurnFailure(voice) {
		t.Fatal("a second failure inside the debounce window must stay silent")
	}
}
