package server

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/server/config"
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

// stubLook records the look sequence. photoErr fails the snapshot; claimErr fails the hold.
func stubLook(t *testing.T, claimErr, photoErr error) *[]string {
	t.Helper()
	var steps []string
	prevClaim, prevRelease, prevSnap, prevCue, prevSees := lookClaimHold, lookReleaseHold, lookSnapshot, lookSayCue, lookModelSeesImages
	lookClaimHold = func() error { steps = append(steps, "hold"); return claimErr }
	lookReleaseHold = func() error { steps = append(steps, "release"); return nil }
	lookSnapshot = func(int, int) (string, error) { steps = append(steps, "photo"); return "/tmp/look.jpg", photoErr }
	lookSayCue = func(pool string) bool { steps = append(steps, "cue:"+pool); return false }
	lookModelSeesImages = func(*config.Config) bool { return true }
	t.Cleanup(func() {
		lookClaimHold, lookReleaseHold, lookSnapshot, lookSayCue, lookModelSeesImages = prevClaim, prevRelease, prevSnap, prevCue, prevSees
	})
	return &steps
}

func runLook(t *testing.T) int {
	t.Helper()
	gin.SetMode(gin.TestMode)
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodPost, "/api/vision/look", strings.NewReader(`{"question":"what is on the left?"}`))
	c.Request.Header.Set("Content-Type", "application/json")
	(&Server{}).lookAndDescribe(c)
	return w.Code
}

func TestLookHoldsFromTheCueThroughThePhoto(t *testing.T) {
	steps := stubLook(t, nil, nil)
	if code := runLook(t); code != http.StatusOK {
		t.Fatalf("status %d", code)
	}
	want := []string{"hold", "cue:look_capturing_main", "photo", "cue:look_analyzing", "release"}
	if !reflect.DeepEqual(*steps, want) {
		t.Fatalf("sequence %v, want %v", *steps, want)
	}
}

func TestLookReleasesTheHoldWhenThePhotoFails(t *testing.T) {
	steps := stubLook(t, nil, errors.New("camera gone"))
	if code := runLook(t); code != http.StatusBadGateway {
		t.Fatalf("status %d", code)
	}
	want := []string{"hold", "cue:look_capturing_main", "photo", "release"}
	if !reflect.DeepEqual(*steps, want) {
		t.Fatalf("sequence %v, want %v", *steps, want)
	}
}

func TestLookRunsUnheldWhenTheHoldIsUnavailable(t *testing.T) {
	// An older HAL has no /servo/hold/claim (404): look as before, release nothing.
	steps := stubLook(t, errors.New("POST /servo/hold/claim returned 404"), nil)
	if code := runLook(t); code != http.StatusOK {
		t.Fatalf("status %d", code)
	}
	want := []string{"hold", "cue:look_capturing_main", "photo", "cue:look_analyzing"}
	if !reflect.DeepEqual(*steps, want) {
		t.Fatalf("sequence %v, want %v", *steps, want)
	}
}
