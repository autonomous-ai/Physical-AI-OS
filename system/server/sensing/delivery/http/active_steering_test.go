package http

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
)

type capabilityBusyGateway struct {
	steeringBusyGateway
	enabled bool
}

func (g *capabilityBusyGateway) SupportsActiveTurnSteering() bool { return g.enabled }
func (g *capabilityBusyGateway) MarkWebChatRun(string)            {}

// LIVE delegates arriving as voice reach a busy steerable runtime.
func TestBusyRuntimeSteeringAdmission(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, eventType := range []string{"voice", "web_chat", "presence.enter"} {
		for _, enabled := range []bool{false, true} {
			name := eventType + "/legacy"
			if enabled {
				name = eventType + "/steering"
			}
			t.Run(name, func(t *testing.T) {
				gw := &capabilityBusyGateway{enabled: enabled}
				h := &SensingHandler{agentGateway: gw, monitorBus: monitor.ProvideBus(), config: &config.Config{}}
				rec := httptest.NewRecorder()
				ctx, _ := gin.CreateTestContext(rec)
				body := `{"type":"` + eventType + `","message":"Check memory again","voice_turn_type":"voice_followup"}`
				ctx.Request = httptest.NewRequest(http.MethodPost, "/api/sensing/event", bytes.NewBufferString(body))
				ctx.Request.Header.Set("Content-Type", "application/json")
				h.PostEvent(ctx)
				if rec.Code != http.StatusOK {
					t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
				}
				wantSent := 0
				if enabled && eventType != "presence.enter" {
					wantSent = 1
				}
				if gw.sent != wantSent || gw.queued != 1-wantSent {
					t.Fatalf("sent=%d queued=%d; want sent=%d queued=%d", gw.sent, gw.queued, wantSent, 1-wantSent)
				}
			})
		}
	}
}
