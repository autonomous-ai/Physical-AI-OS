package http

import (
	"encoding/json"
	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/intent"
	"go.autonomous.ai/os/system/intent/jev"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

type contextGateway struct {
	idleGateway
	checked bool
	sent    string
}

func (g *contextGateway) IsReady() bool { g.checked = true; return true }

func (g *contextGateway) NextChatRunID() (string, string) { return "context-req", "context-run" }
func (g *contextGateway) MarkWebChatRun(string)           {}
func (g *contextGateway) SendChatMessageWithRun(text, req, run string) (string, error) {
	g.sent = text
	return run, nil
}

func TestIntentContextRouting(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, kind := range []string{"voice", "voice_command", "voice_followup", "web_chat", "mqtt_chat"} {
		for _, tc := range []struct {
			name, text                     string
			pending, hint, local, provider bool
			response                       int
		}{
			{"render fragment", "brighter", true, false, false, false, 200},
			{"followup hint", "make it brighter", false, true, false, false, 200},
			{"preparation unknown", "continue", false, false, false, false, 200},
			{"expired hint pending task", "make it brighter", true, false, false, false, 200},
			{"explicit lamp", "tăng sáng đèn Lamp", true, true, true, true, 200},
			{"connection alone", "turn off the light", false, false, true, false, 200},
			{"new digital abstain", "Create a landscape render", false, false, false, true, 200},
			{"new digital error", "Create a landscape render", false, false, false, true, 503},
		} {
			t.Run(kind+"/"+tc.name, func(t *testing.T) {
				previous := http.DefaultTransport
				defer func() { http.DefaultTransport = previous }()
				intent.Configure(map[string]bool{device.CapLight: true})
				defer intent.Configure(nil)
				calls, writes := 0, 0
				spoken := make(chan struct{}, 1)
				http.DefaultTransport = jevTestTransport(func(r *http.Request) (*http.Response, error) {
					body := `{}`
					status := 200
					if r.URL.Host == "proxy.test" {
						calls++
						status = tc.response
						var payload struct {
							State struct {
								Candidates []jev.Candidate `json:"candidates"`
							} `json:"state"`
						}
						_ = json.NewDecoder(r.Body).Decode(&payload)
						choice := "none"
						if tc.local {
							choice = "scene_energize"
						}
						probabilities := map[string]float64{"none": 0}
						answers := map[string]any{}
						for _, candidate := range payload.State.Candidates {
							probabilities[candidate.ID] = 0
							answers["fit_"+candidate.ID] = map[string]any{"type": "noul", "noul": 1}
						}
						probabilities[choice] = 1
						answers["intent"] = map[string]any{"type": "choice", "choice": choice, "probabilities": probabilities}
						encoded, _ := json.Marshal(map[string]any{"answers": answers})
						body = string(encoded)
					} else if r.URL.Path == "/voice/speak" {
						spoken <- struct{}{}
					} else {
						writes++
					}
					return &http.Response{StatusCode: status, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(body))}, nil
				})
				gw := &contextGateway{}
				h := &SensingHandler{agentGateway: gw, monitorBus: monitor.ProvideBus(), config: &config.Config{LLMBaseURL: "https://proxy.test", LLMAPIKey: "test"}, intentResolver: jev.NewResolver(), harnessConnected: func() bool { return true }, harnessTaskPending: func() bool { return tc.pending }, harnessFollowup: func() bool { return tc.hint }}
				h.lastNotReadyTTS.Store(time.Now().UnixMilli())
				rec := httptest.NewRecorder()
				c, _ := gin.CreateTestContext(rec)
				message := tc.text
				if strings.HasPrefix(kind, "voice") {
					message = "[voice-instruction] " + message + "\n[transcript] " + message
				}
				payload, _ := json.Marshal(map[string]string{"type": kind, "message": message})
				c.Request = httptest.NewRequest("POST", "/api/sensing/event", strings.NewReader(string(payload)))
				c.Request.Header.Set("Content-Type", "application/json")
				h.PostEvent(c)
				defer DefaultFillerManager.Cancel("context-run")
				if tc.local && strings.HasPrefix(kind, "voice") {
					select {
					case <-spoken:
					case <-time.After(time.Second):
						t.Fatal("missing voice response")
					}
				}
				local := strings.Contains(rec.Body.String(), `"handler":"local"`)
				if local != tc.local || gw.checked == tc.local || (!tc.local && !strings.Contains(gw.sent, tc.text)) || (calls > 0) != tc.provider || (!tc.local && writes != 0) {
					t.Fatalf("local=%v main=%v provider=%d writes=%d response=%s", local, gw.checked, calls, writes, rec.Body.String())
				}
			})
		}
	}
}
