package http

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/skillcontext/mood"
)

// postSensingEvent drives PostEvent through the mood.CurrentUser sync.
func postSensingEvent(t *testing.T, h *SensingHandler, eventType, currentUser string) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	body, err := json.Marshal(map[string]string{
		"type":         eventType,
		"message":      "Speech emotion detected: Sad.",
		"current_user": currentUser,
	})
	if err != nil {
		t.Fatalf("marshal body: %v", err)
	}
	c.Request = httptest.NewRequest(http.MethodPost, "/api/sensing/event", bytes.NewBuffer(body))
	c.Request.Header.Set("Content-Type", "application/json")
	h.PostEvent(c)
}

func newSyncTestHandler() *SensingHandler {
	return &SensingHandler{
		agentGateway: &busyGateway{},
		monitorBus:   monitor.ProvideBus(),
		config:       &config.Config{},
	}
}

// The defect: SER identifies nobody.
func TestSpeechEmotionUnknownDoesNotClobberCurrentUser(t *testing.T) {
	mood.SetCurrentUser("long") // as a face detection would have set it
	t.Cleanup(mood.ClearCurrentUser)

	postSensingEvent(t, newSyncTestHandler(), "speech_emotion.detected", "unknown")

	if got := mood.CurrentUser(); got != "long" {
		t.Fatalf("face-derived identity was clobbered: mood.CurrentUser() = %q, want %q", got, "long")
	}
}

// SER never writes the current user, even for a confident speaker.
func TestSpeechEmotionNeverSetsCurrentUserEvenWhenIdentified(t *testing.T) {
	mood.SetCurrentUser("long")
	t.Cleanup(mood.ClearCurrentUser)

	postSensingEvent(t, newSyncTestHandler(), "speech_emotion.detected", "mai")

	if got := mood.CurrentUser(); got != "long" {
		t.Fatalf("SER must never drive presence: mood.CurrentUser() = %q, want %q", got, "long")
	}
}

// Guard against over-reach: the exemption must be scoped to SER alone.
func TestOtherSensingEventsStillSyncCurrentUser(t *testing.T) {
	mood.SetCurrentUser("long")
	t.Cleanup(mood.ClearCurrentUser)

	postSensingEvent(t, newSyncTestHandler(), "emotion.detected", "mai")

	if got := mood.CurrentUser(); got != "mai" {
		t.Fatalf("non-SER events must still sync: mood.CurrentUser() = %q, want %q", got, "mai")
	}
}

// presence.leave still reaches ClearCurrentUser.
func TestPresenceLeaveStillClearsCurrentUser(t *testing.T) {
	mood.SetCurrentUser("long")
	t.Cleanup(mood.ClearCurrentUser)

	postSensingEvent(t, newSyncTestHandler(), "presence.leave", "")

	if got := mood.CurrentUser(); got != "" {
		t.Fatalf("presence.leave must clear: mood.CurrentUser() = %q, want empty", got)
	}
}
