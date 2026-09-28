package http

import (
	"context"
	"encoding/json"
	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/monitor"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/intent"
	"go.autonomous.ai/os/system/intent/jev"
	"go.autonomous.ai/os/system/server/config"
)

type jevTestTransport func(*http.Request) (*http.Response, error)

func (f jevTestTransport) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }

func TestVoiceIntentWiresJevFlagKeyAndFallback(t *testing.T) {
	intent.Configure(map[string]bool{device.CapLight: true})
	t.Cleanup(func() { intent.Configure(nil) })

	previous := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = previous })
	providerCalls, halCalls := 0, 0
	color := [3]int{160, 120, 80}
	http.DefaultTransport = jevTestTransport(func(req *http.Request) (*http.Response, error) {
		body := `{}`
		if req.URL.Host == "proxy.example.test" {
			providerCalls++
			if req.URL.Path != "/api/v1/ai/v1/jev/decisions" {
				t.Error("wrong BFF route")
			}
			if req.Header.Get("Authorization") != "Bearer test-jev-key" {
				t.Error("configured device key not used")
			}
			if _, ok := req.Context().Deadline(); !ok {
				t.Error("missing decision deadline")
			}
			var payload struct {
				State struct {
					Candidates []jev.Candidate `json:"candidates"`
				} `json:"state"`
			}
			if err := json.NewDecoder(req.Body).Decode(&payload); err != nil {
				t.Fatal(err)
			}
			probabilities := map[string]float64{"none": 0.01}
			answers := map[string]any{}
			for _, candidate := range payload.State.Candidates {
				if candidate.ID == "servo_track" || candidate.ID == "music_stop" {
					t.Fatalf("unavailable capability offered: %s", candidate.ID)
				}
				probabilities[candidate.ID] = 0
				answers["fit_"+candidate.ID] = map[string]any{"type": "noul", "noul": 0.01}
			}
			if len(payload.State.Candidates) != 12 {
				t.Fatalf("expected light commands and current time, got %d candidates", len(payload.State.Candidates))
			}
			probabilities["dim"] = 0.99
			answers["intent"] = map[string]any{"type": "choice", "choice": "dim", "probabilities": probabilities}
			answers["fit_dim"] = map[string]any{"type": "noul", "noul": 0.99}
			encoded, err := json.Marshal(map[string]any{"answers": answers})
			if err != nil {
				t.Fatal(err)
			}
			body = string(encoded)
		} else {
			if req.URL.Path == "/emotion/status" {
				return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"sleeping":false}`))}, nil
			}
			halCalls++
			if req.URL.Path == "/led/color" {
				encoded, _ := json.Marshal(map[string]any{"color": color})
				body = string(encoded)
			} else if req.URL.Path == "/led/solid" {
				var payload struct {
					Color [3]int `json:"color"`
				}
				_ = json.NewDecoder(req.Body).Decode(&payload)
				color = payload.Color
			} else {
				t.Errorf("unexpected HAL action %s", req.URL.Path)
			}
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(body))}, nil
	})
	enabled := false
	cfg := &config.Config{LLMBaseURL: "https://proxy.example.test/api/v1/ai/v1", LLMAPIKey: "test-jev-key", JevIntent: &config.JevIntentConfig{Enabled: &enabled}}
	h := &SensingHandler{config: cfg, intentResolver: jev.NewResolver()}
	if h.matchVoiceIntent(context.Background(), "This lamp is too bright.") != nil || providerCalls != 0 || halCalls != 0 {
		t.Fatal("disabled path contacted provider or executed")
	}
	enabled = true
	got := h.matchVoiceIntent(context.Background(), "This lamp is too bright.")
	if got == nil || got.Source != "jev" || got.Rule != "dim" || providerCalls != 1 || halCalls != 3 {
		t.Fatalf("enabled path failed: %+v, provider=%d HAL=%d", got, providerCalls, halCalls)
	}
	gin.SetMode(gin.TestMode)
	h.monitorBus = monitor.ProvideBus()
	h.agentGateway = &idleGateway{}
	for _, eventType := range []string{"web_chat", "mqtt_chat"} {
		t.Run(eventType, func(t *testing.T) {
			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			c.Request = httptest.NewRequest(http.MethodPost, "/api/sensing/event", strings.NewReader(`{"type":"`+eventType+`","message":"This lamp is too bright."}`))
			c.Request.Header.Set("Content-Type", "application/json")
			h.PostEvent(c)
			var reply struct {
				Status int               `json:"status"`
				Data   map[string]string `json:"data"`
			}
			if err := json.Unmarshal(rec.Body.Bytes(), &reply); err != nil {
				t.Fatal(err)
			}
			if rec.Code != 200 || reply.Status != 1 || reply.Data["handler"] != "local" || reply.Data["response"] == "" || reply.Data["localRunId"] == "" || reply.Data["runId"] != "" {
				t.Fatalf("chat did not return a correlated immediate reply: %s", rec.Body.String())
			}
		})
	}
	if providerCalls != 3 || halCalls != 9 {
		t.Fatalf("chat did not use Jev exactly once per turn: provider=%d HAL=%d", providerCalls, halCalls)
	}
	cfg.LLMAPIKey = ""
	if h.matchVoiceIntent(context.Background(), "This lamp is too bright.") != nil || providerCalls != 3 || halCalls != 9 {
		t.Fatal("missing key did not fall through")
	}
}
