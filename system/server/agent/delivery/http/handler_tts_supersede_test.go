package http

import (
	"fmt"
	"github.com/gin-gonic/gin"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
)

func TestDelayedCaptureCancelOnlyMutesOlderSpeech(t *testing.T) {
	h := newCancelTestHandler()
	cutoff := time.Now().Add(-time.Second)
	old := deviceRunID(1, cutoff.Add(-time.Second))
	next := deviceRunID(2, cutoff.Add(time.Millisecond))
	h.cancelSpeechBefore(cutoff.UnixMilli())
	if !h.isSpeechCancelled(old) || h.isSpeechCancelled(next) {
		t.Fatal("cutoff did not isolate old reply")
	}
	if h.isHWCancelled(old) {
		t.Fatal("superseding speech must not abort agent hardware work")
	}
	h.cancelSpeechBefore(cutoff.Add(-time.Second).UnixMilli())
	if h.autoSpeechWatermarkMs.Load() != cutoff.UnixMilli() {
		t.Fatal("older request moved cutoff backwards")
	}
}

func TestScopedCancelHandlerDoesNotCallGlobalStop(t *testing.T) {
	h := newCancelTestHandler() // nil gateway would panic on global StopTTS
	body := `{"before_ms":` + fmt.Sprint(time.Now().Add(-time.Second).UnixMilli()) + `}`
	w := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(w)
	c.Request = httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	h.CancelSpeechHandler(c)
	if w.Code != http.StatusOK {
		t.Fatal(w.Code, w.Body.String())
	}
}

// Realtime answering a newer utterance mutes a stale chit-chat reply but never
// the answer to a task the user is still waiting on.
func TestRealtimeSupersedeKeepsDelegatedTaskReplies(t *testing.T) {
	t.Setenv("OS_REALTIME_SUPERSEDES_MAIN_REPLY", "1")
	h := newCancelTestHandler()
	// A fresh filler manager: earlier tests leave supersede cutoffs on the shared one.
	origFM := sensinghttp.DefaultFillerManager
	sensinghttp.DefaultFillerManager = sensinghttp.NewFillerManager()
	defer func() { sensinghttp.DefaultFillerManager = origFM }()
	stale := deviceRunID(11, time.Now().Add(-5*time.Second))
	task := deviceRunID(12, time.Now().Add(-4*time.Second))
	sensinghttp.DefaultFillerManager.MarkVoiceRun(stale, "vi-stale")
	sensinghttp.DefaultFillerManager.MarkDelegatedVoiceRun(task, "vi-task")
	defer sensinghttp.DefaultFillerManager.Cancel(stale)
	defer sensinghttp.DefaultFillerManager.Cancel(task)

	if !h.CancelSpeechForNewerTurn() {
		t.Fatal("supersede should be armed by the environment flag")
	}
	if !h.isSpeechCancelled(stale) {
		t.Fatal("the stale chit-chat reply should be muted")
	}
	if h.isSpeechCancelled(task) {
		t.Fatal("the delegated task's reply must still be spoken")
	}
	// The task mark survives the filler cancel that precedes every TTS send.
	sensinghttp.DefaultFillerManager.Cancel(task)
	if h.isSpeechCancelled(task) {
		t.Fatal("the task mark must outlive the filler state")
	}
}
