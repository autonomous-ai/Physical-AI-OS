package mqtthandler

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

type ttsPreviewSpeedTransport func(*http.Request) (*http.Response, error)

func (f ttsPreviewSpeedTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}

func TestTTSPreviewMQTTSpeed(t *testing.T) {
	type halRequest struct {
		method string
		path   string
		body   map[string]any
		err    error
	}
	requests := make(chan halRequest, 16)
	original := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = original })
	http.DefaultTransport = ttsPreviewSpeedTransport(func(r *http.Request) (*http.Response, error) {
		var body map[string]any
		err := json.NewDecoder(r.Body).Decode(&body)
		requests <- halRequest{method: r.Method, path: r.URL.Path, body: body, err: err}
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{}`))}, nil
	})
	factory, messages := statusBroker(t)
	savedSpeed := 1.2
	cfg := &config.Config{DeviceID: "tts-preview-test", FDChannel: "test/fd", TTSSpeed: &savedSpeed}
	h := &DeviceMQTTHandler{config: cfg, mqttFactory: factory}
	readStatus := func(t *testing.T, want string) {
		t.Helper()
		select {
		case payload := <-messages:
			var reply domain.MQTTDataResponse
			if err := json.Unmarshal(payload, &reply); err != nil {
				t.Fatal(err)
			}
			if reply.Kind != domain.KindTTSPreview || reply.Status != want {
				t.Fatalf("unexpected MQTT reply: %s; want status %q", payload, want)
			}
			if want == "failure" && reply.Error == "" {
				t.Fatalf("failure reply has no error: %s", payload)
			}
		case <-time.After(3 * time.Second):
			t.Fatalf("missing MQTT %s reply", want)
		}
	}
	for _, tc := range []struct {
		name      string
		speedJSON string
		wantSpeed any
		invalid   bool
	}{
		{name: "explicit speed", speedJSON: `,"speed":1.5`, wantSpeed: 1.5},
		{name: "lower bound", speedJSON: `,"speed":0.25`, wantSpeed: 0.25},
		{name: "upper bound", speedJSON: `,"speed":4`, wantSpeed: float64(4)},
		{name: "omitted speed"},
		{name: "null speed", speedJSON: `,"speed":null`},
		{name: "zero speed", speedJSON: `,"speed":0`, invalid: true},
		{name: "above upper bound", speedJSON: `,"speed":4.1`, invalid: true},
		{name: "string speed", speedJSON: `,"speed":"1.5"`, invalid: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			data := json.RawMessage(`{"text":"Preview voice","voice":"test-voice","provider":"openai"` + tc.speedJSON + `}`)
			if err := h.dispatchData(domain.MQTTDataCommand{Kind: domain.KindTTSPreview, Data: data}); err != nil {
				t.Fatal(err)
			}
			if tc.invalid {
				readStatus(t, "failure")
				select {
				case got := <-requests:
					t.Fatalf("invalid speed reached HAL: %+v", got)
				default:
				}
			} else {
				readStatus(t, "starting")
				readStatus(t, "success")
				select {
				case got := <-requests:
					if got.err != nil {
						t.Fatal(got.err)
					}
					if got.method != http.MethodPost || got.path != "/voice/speak" || got.body["text"] != "Preview voice" || got.body["voice"] != "test-voice" || got.body["provider"] != "openai" {
						t.Fatalf("unexpected HAL request: %+v", got)
					}
					speed, present := got.body["speed"]
					if speed != tc.wantSpeed || present != (tc.wantSpeed != nil) {
						t.Fatalf("HAL speed = %v (present %t), want %v", speed, present, tc.wantSpeed)
					}
				default:
					t.Fatal("successful preview did not reach HAL")
				}
			}
			if cfg.TTSSpeed == nil || cfg.GetTTSSpeed() != 1.2 {
				t.Fatalf("preview changed saved speed: %v", cfg.TTSSpeed)
			}
		})
	}
}
