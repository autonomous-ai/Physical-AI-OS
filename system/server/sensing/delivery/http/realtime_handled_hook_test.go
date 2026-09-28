package http

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
)

// busyGateway implements only the methods this path calls.
type busyGateway struct {
	domain.AgentGateway
	queued int
}

func (g *busyGateway) IsBusy() bool { return true }
func (g *busyGateway) Name() string { return "fake" }
func (g *busyGateway) QueuePendingEvent(eventType, msg string, images []string, fixedRunID string) {
	g.queued++
}

type steeringBusyGateway struct {
	busyGateway
	sent       int
	silentRuns int
}

func (g *steeringBusyGateway) SupportsActiveTurnSteering() bool { return true }
func (g *steeringBusyGateway) IsReady() bool                    { return true }
func (g *steeringBusyGateway) NextChatRunID() (string, string)  { return "req-handled", "run-handled" }
func (g *steeringBusyGateway) MarkSilentRun(string)             { g.silentRuns++ }
func (g *steeringBusyGateway) SendChatMessageWithRun(string, string, string) (string, error) {
	g.sent++
	return "run-handled", nil
}

func postRealtimeHandled(t *testing.T, h *SensingHandler, voiceType ...string) *httptest.ResponseRecorder {
	t.Helper()
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	body := `{"type":"voice_agent_handled","message":"[HANDLED] what time is it\n[REPLY] just past two"}`
	if len(voiceType) > 0 {
		var payload map[string]any
		if err := json.Unmarshal([]byte(body), &payload); err != nil {
			t.Fatal(err)
		}
		payload["voice_turn_type"] = voiceType[0]
		encoded, err := json.Marshal(payload)
		if err != nil {
			t.Fatal(err)
		}
		body = string(encoded)
	}
	c.Request = httptest.NewRequest(http.MethodPost, "/api/sensing/event", bytes.NewBufferString(body))
	c.Request.Header.Set("Content-Type", "application/json")
	h.PostEvent(c)
	return rec
}

// The regression this whole hook exists for.
func TestRealtimeHandledHookFiresEvenWhenTheAgentIsBusy(t *testing.T) {
	gw := &busyGateway{}
	h := &SensingHandler{agentGateway: gw, monitorBus: monitor.ProvideBus(), config: &config.Config{}}
	fired := 0
	h.SetOnRealtimeHandled(func() bool { fired++; return true })

	rec := postRealtimeHandled(t, h)

	if fired != 1 {
		t.Fatalf("hook must fire before the busy fork returns; fired=%d (status %d)", fired, rec.Code)
	}
	if gw.queued != 1 {
		t.Errorf("the sync event itself must still be queued for replay, queued=%d", gw.queued)
	}
}

func TestRealtimeHandledSteersInsteadOfQueuingBehindCodex(t *testing.T) {
	gw := &steeringBusyGateway{}
	h := &SensingHandler{agentGateway: gw, monitorBus: monitor.ProvideBus(), config: &config.Config{}}

	rec := postRealtimeHandled(t, h)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if gw.queued != 0 {
		t.Fatalf("realtime history sync must steer, not queue; queued=%d", gw.queued)
	}
	if gw.sent != 1 || gw.silentRuns != 1 {
		t.Fatalf("expected one silent steered sync, sent=%d silent=%d", gw.sent, gw.silentRuns)
	}
}

// The hook is optional wiring — a handler without it must behave as before.
func TestPostEventWithoutHookIsUnaffected(t *testing.T) {
	gw := &busyGateway{}
	h := &SensingHandler{agentGateway: gw, monitorBus: monitor.ProvideBus(), config: &config.Config{}}

	rec := postRealtimeHandled(t, h)

	if rec.Code != http.StatusOK {
		t.Errorf("expected 200 with no hook installed, got %d", rec.Code)
	}
}

// The response tells HAL whether the older turn actually lost the speaker.
func TestRealtimeHandledResponseReportsWhetherSpeechWasSuppressed(t *testing.T) {
	for _, tc := range []struct {
		name      string
		applied   bool
		wantValue bool
	}{
		{"policy on", true, true},
		{"policy off", false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			gw := &busyGateway{}
			h := &SensingHandler{agentGateway: gw, monitorBus: monitor.ProvideBus(), config: &config.Config{}}
			h.SetOnRealtimeHandled(func() bool { return tc.applied })

			rec := postRealtimeHandled(t, h)

			var body struct {
				Data map[string]any `json:"data"`
			}
			if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
				t.Fatalf("decode response: %v (%s)", err, rec.Body.String())
			}
			got, ok := body.Data["speechSuppressed"]
			if !ok {
				t.Fatalf("response must carry speechSuppressed: %s", rec.Body.String())
			}
			if got != tc.wantValue {
				t.Errorf("speechSuppressed = %v, want %v", got, tc.wantValue)
			}
		})
	}
}
