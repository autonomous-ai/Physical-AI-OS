package analytics

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/joho/godotenv"
)

func TestTrackEvent(t *testing.T) {
	var gotAuth string
	var gotBody map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		_ = json.NewDecoder(r.Body).Decode(&gotBody)
	}))
	defer srv.Close()

	t.Setenv(envKey, "test-key")
	t.Setenv("AUTONOMOUS_ANALYTICS_URL", srv.URL)

	if err := TrackEvent(context.Background(), "device_booted", map[string]any{
		"device_type": "lamp",
		"skipped":     nil,
	}); err != nil {
		t.Fatalf("TrackEvent: %v", err)
	}

	if gotAuth != "test-key" {
		t.Errorf("Authorization = %q, want test-key", gotAuth)
	}
	if gotBody["event_name"] != "device_booted" {
		t.Errorf("event_name = %v", gotBody["event_name"])
	}
	data := gotBody["data"].(map[string]any)
	if data["platform"] != platform {
		t.Errorf("platform = %v", data["platform"])
	}
	params := data["event_params"].([]any)
	if len(params) != 1 {
		t.Fatalf("event_params = %v, want nil value dropped", params)
	}
	p := params[0].(map[string]any)
	if p["key"] != "device_type" || p["value"] != "lamp" {
		t.Errorf("event_params[0] = %v", p)
	}
}

// Key and endpoint come from the body's .env.
func TestEndpointFromEnvFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ".env")
	if err := os.WriteFile(path, []byte("AUTONOMOUS_ANALYTICS_ID=k\nAUTONOMOUS_ANALYTICS_URL=https://staging.example/api\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	kv, err := godotenv.Read(path)
	if err != nil {
		t.Fatalf("read env file: %v", err)
	}
	origKey, origURL := apiKey, fileURL
	apiKey, fileURL = kv["AUTONOMOUS_ANALYTICS_ID"], kv["AUTONOMOUS_ANALYTICS_URL"]
	t.Cleanup(func() { apiKey, fileURL = origKey, origURL })

	if got := Endpoint(); got != "https://staging.example/api" {
		t.Errorf("Endpoint() = %q, want the .env value", got)
	}
	// The process env still wins.
	t.Setenv("AUTONOMOUS_ANALYTICS_URL", "http://127.0.0.1:1/override")
	if got := Endpoint(); got != "http://127.0.0.1:1/override" {
		t.Errorf("Endpoint() = %q, want the process env to win", got)
	}
}

// No endpoint anywhere means analytics is off; there is no built-in default.
func TestEndpointIsEmptyWhenNothingIsConfigured(t *testing.T) {
	origURL := fileURL
	fileURL = ""
	t.Setenv("AUTONOMOUS_ANALYTICS_URL", "")
	t.Cleanup(func() { fileURL = origURL })
	if got := Endpoint(); got != "" {
		t.Errorf("Endpoint() = %q, want empty", got)
	}
}

func TestTrackEventRefusesWithoutAnEndpoint(t *testing.T) {
	origURL, origKey := fileURL, apiKey
	fileURL, apiKey = "", "k"
	t.Setenv("AUTONOMOUS_ANALYTICS_URL", "")
	t.Cleanup(func() { fileURL, apiKey = origURL, origKey })
	if err := TrackEvent(context.Background(), "x", nil); err == nil {
		t.Error("TrackEvent must fail loudly when no endpoint is configured")
	}
}
