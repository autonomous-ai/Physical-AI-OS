package http

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/speakergate"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
)

// idleGateway has no turn in flight while the speaker may still be busy.
type idleGateway struct {
	domain.AgentGateway
	queued  atomic.Int32
	drained atomic.Int32
}

func (g *idleGateway) IsBusy() bool { return false }
func (g *idleGateway) Name() string { return "fake" }
func (g *idleGateway) QueuePendingEvent(eventType, msg string, images []string, fixedRunID string) {
	g.queued.Add(1)
}
func (g *idleGateway) DrainPendingEvents() { g.drained.Add(1) }

// Not ready, so forwarding stops right after the branch under test.
func (g *idleGateway) IsReady() bool { return false }

func postPresenceEnter(t *testing.T, h *SensingHandler) *httptest.ResponseRecorder {
	t.Helper()
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	body := `{"type":"presence.enter","message":"Person detected — 1 face(s) visible"}`
	c.Request = httptest.NewRequest(http.MethodPost, "/api/sensing/event", bytes.NewBufferString(body))
	c.Request.Header.Set("Content-Type", "application/json")
	h.PostEvent(c)
	return rec
}

func withSpeaker(t *testing.T, busy func() bool) {
	t.Helper()
	orig := speakergate.SpeakerBusy
	speakergate.SpeakerBusy = busy
	t.Cleanup(func() { speakergate.SpeakerBusy = orig })
}

// An idle runtime still playing its reply must not be cut off by a passive event.
func TestPresenceEnterWaitsWhileTheDeviceIsStillSpeaking(t *testing.T) {
	var busy atomic.Bool
	busy.Store(true)
	withSpeaker(t, busy.Load)

	gw := &idleGateway{}
	h := &SensingHandler{agentGateway: gw, monitorBus: monitor.ProvideBus(), config: &config.Config{}}

	rec := postPresenceEnter(t, h)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if got := gw.queued.Load(); got != 1 {
		t.Fatalf("event must be queued while the speaker is busy, queued=%d", got)
	}
	if got := gw.drained.Load(); got != 0 {
		t.Fatalf("nothing may replay while the speaker is still busy, drained=%d", got)
	}

	busy.Store(false)
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) && gw.drained.Load() == 0 {
		time.Sleep(10 * time.Millisecond)
	}
	if got := gw.drained.Load(); got == 0 {
		t.Fatal("the queued event was never replayed after the speaker went idle")
	}
}

// An idle device must behave exactly as before: straight through, no queue.
func TestPresenceEnterGoesStraightThroughWhenTheSpeakerIsIdle(t *testing.T) {
	withSpeaker(t, func() bool { return false })

	gw := &idleGateway{}
	h := &SensingHandler{agentGateway: gw, monitorBus: monitor.ProvideBus(), config: &config.Config{}}

	_ = postPresenceEnter(t, h)

	if got := gw.queued.Load(); got != 0 {
		t.Fatalf("an idle speaker must not queue anything, queued=%d", got)
	}
}
