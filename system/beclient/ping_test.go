package beclient

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"go.autonomous.ai/os/system/server/config"
)

func TestPingPayloadIncludesWakeWordState(t *testing.T) {
	for _, want := range []bool{true, false} {
		payload, err := json.Marshal(PingPayload{WakeWordEnabled: want})
		if err != nil {
			t.Fatalf("marshal ping payload: %v", err)
		}
		var fields map[string]json.RawMessage
		if err := json.Unmarshal(payload, &fields); err != nil {
			t.Fatalf("unmarshal ping payload: %v", err)
		}

		raw, ok := fields["wakeword_enabled"]
		if !ok {
			t.Fatal("wakeword_enabled must be present even when disabled")
		}
		var got bool
		if err := json.Unmarshal(raw, &got); err != nil {
			t.Fatalf("unmarshal wakeword_enabled: %v", err)
		}
		if got != want {
			t.Fatalf("wakeword_enabled = %t, want %t", got, want)
		}
	}
}

func TestPingUsesBackendOverride(t *testing.T) {
	var gotPath, gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath, gotAuth = r.URL.Path, r.Header.Get("Authorization")
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()
	cfg := &config.Config{
		LLMBaseURL:     "http://127.0.0.1:1/v1", // local model server, must not be hit
		LLMAPIKey:      "ollama",
		BackendBaseURL: srv.URL + "/api/v1/ai/v1",
		BackendAPIKey:  "backend-key",
		MQTTEndpoint:   "mqtt.example",
	}
	if _, err := New(cfg).Ping(cfg.BackendKey(), PingPayload{}); err != nil {
		t.Fatalf("ping: %v", err)
	}
	if gotPath != "/api/v1/ai/ping" {
		t.Fatalf("path = %q, want /api/v1/ai/ping", gotPath)
	}
	if gotAuth != "Bearer backend-key" {
		t.Fatalf("auth = %q, want backend key", gotAuth)
	}
}

func TestBackendFallsBackToLLM(t *testing.T) {
	cfg := &config.Config{LLMBaseURL: "https://x/v1", LLMAPIKey: "k"}
	if cfg.BackendBase() != "https://x/v1" || cfg.BackendKey() != "k" {
		t.Fatalf("empty backend fields must reuse llm_base_url / llm_api_key")
	}
}

func TestPingIncludesVoiceInputMode(t *testing.T) {
	for _, mode := range []string{"automatic", "tap_to_talk"} {
		data, err := json.Marshal(PingPayload{VoiceInputMode: mode})
		if err != nil {
			t.Fatal(err)
		}
		var fields map[string]any
		if err := json.Unmarshal(data, &fields); err != nil {
			t.Fatal(err)
		}
		if fields["voice_input_mode"] != mode {
			t.Fatalf("mode=%v", fields["voice_input_mode"])
		}
	}
}
