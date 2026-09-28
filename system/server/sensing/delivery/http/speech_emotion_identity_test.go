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

// postSensingEvent drives PostEvent far enough to exercise the mood.CurrentUser
// sync at the top of the handler. It reuses busyGateway (realtime_handled_hook_test.go)
// so the passive-event fork returns early at the queue step — well after the
// sync, and before anything that would need a real agent.
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

// The defect: SER identifies nobody. Its current_user is a courier value from
// the voice turn, and five unrelated situations collapse into "unknown" —
// speaker-ID found no match, speaker-ID could not run, no transcript at all,
// the wake-word gate rejected the turn, or the noise guard dropped it. Before
// the exemption, one ambient sigh from an unrecognized voice erased a live
// face-derived identity for every downstream reader of mood.CurrentUser().
func TestSpeechEmotionUnknownDoesNotClobberCurrentUser(t *testing.T) {
	mood.SetCurrentUser("long") // as a face detection would have set it
	t.Cleanup(mood.ClearCurrentUser)

	postSensingEvent(t, newSyncTestHandler(), "speech_emotion.detected", "unknown")

	if got := mood.CurrentUser(); got != "long" {
		t.Fatalf("face-derived identity was clobbered: mood.CurrentUser() = %q, want %q", got, "long")
	}
}

// The exemption is on the event type, not on the string "unknown": SER is never
// a presence authority, so even a confidently-identified speaker must not write
// here. Nothing is lost — a confident speaker-ID match is already promoted
// device-wide by voice_service.py set_voice_user() before the SER event is even
// queued, and every other producer ships it via app_state.resolve_current_user().
func TestSpeechEmotionNeverSetsCurrentUserEvenWhenIdentified(t *testing.T) {
	mood.SetCurrentUser("long")
	t.Cleanup(mood.ClearCurrentUser)

	postSensingEvent(t, newSyncTestHandler(), "speech_emotion.detected", "mai")

	if got := mood.CurrentUser(); got != "long" {
		t.Fatalf("SER must never drive presence: mood.CurrentUser() = %q, want %q", got, "long")
	}
}

// Guard against over-reach: the exemption must be scoped to SER alone. Face
// emotion is the modality that genuinely observes who is present, so it keeps
// writing.
func TestOtherSensingEventsStillSyncCurrentUser(t *testing.T) {
	mood.SetCurrentUser("long")
	t.Cleanup(mood.ClearCurrentUser)

	postSensingEvent(t, newSyncTestHandler(), "emotion.detected", "mai")

	if got := mood.CurrentUser(); got != "mai" {
		t.Fatalf("non-SER events must still sync: mood.CurrentUser() = %q, want %q", got, "mai")
	}
}

// The else-branch must survive the added condition. presence.leave carries no
// current_user, so it still has to reach ClearCurrentUser.
func TestPresenceLeaveStillClearsCurrentUser(t *testing.T) {
	mood.SetCurrentUser("long")
	t.Cleanup(mood.ClearCurrentUser)

	postSensingEvent(t, newSyncTestHandler(), "presence.leave", "")

	if got := mood.CurrentUser(); got != "" {
		t.Fatalf("presence.leave must clear: mood.CurrentUser() = %q, want empty", got)
	}
}
