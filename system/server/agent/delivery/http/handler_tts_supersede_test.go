package http

import (
	"fmt"
	"github.com/gin-gonic/gin"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
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
