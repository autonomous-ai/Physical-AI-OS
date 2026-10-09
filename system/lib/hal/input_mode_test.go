package hal

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestSetVoiceInputModeRequiresHALAcknowledgement(t *testing.T) {
	previous := inputModeClient
	t.Cleanup(func() { inputModeClient = previous })
	for _, tc := range []struct {
		name, body string
		status     int
		wantError  bool
	}{
		{"ready", `{"status":"ok"}`, 200, false},
		{"rejected", `{"detail":"transition unavailable"}`, 503, true},
		{"not ready", `{"status":"pending"}`, 200, true},
		{"bad response", `{`, 200, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			inputModeClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
				var payload struct {
					Mode     string `json:"mode"`
					Wakeword bool   `json:"wakeword"`
				}
				if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
					t.Fatal(err)
				}
				if r.Method != http.MethodPost || r.URL.Path != "/voice/input-mode" || payload.Mode != "tap_to_talk" || !payload.Wakeword {
					t.Fatalf("unexpected request: %s %s %+v", r.Method, r.URL.Path, payload)
				}
				return &http.Response{StatusCode: tc.status, Body: io.NopCloser(strings.NewReader(tc.body))}, nil
			})}
			err := SetVoiceInputMode(context.Background(), "tap_to_talk", true)
			if (err != nil) != tc.wantError {
				t.Fatalf("error = %v, want error %v", err, tc.wantError)
			}
		})
	}
}
