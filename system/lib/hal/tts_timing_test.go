package hal

import (
	"bytes"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"testing"
)

func TestPostSpeakTimingPreservesPayloadAndSuppression(t *testing.T) {
	originalClient, originalLogger := httpClient, slog.Default()
	t.Cleanup(func() { httpClient = originalClient; slog.SetDefault(originalLogger) })
	for _, tc := range []struct {
		name, response string
		wantErr        bool
	}{
		{"accepted", `{"status":"queued"}`, false},
		{"muted", `{"status":"suppressed"}`, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var logs bytes.Buffer
			slog.SetDefault(slog.New(slog.NewJSONHandler(&logs, nil)))
			body := []byte(`{"text":"hello","turn_id":"voice-1","turn_seq":7}`)
			httpClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
				got, _ := io.ReadAll(r.Body)
				if !bytes.Equal(got, body) {
					t.Fatalf("payload changed: %s", got)
				}
				return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(tc.response)), Header: make(http.Header)}, nil
			})}
			err := postSpeak("/voice/speak-queue", body)
			if errors.Is(err, ErrSpeakerMuted) != tc.wantErr {
				t.Fatalf("error = %v", err)
			}
			for _, token := range []string{"hal_post_start", "hal_post_complete", `"text_key":"2cf24dba5fb0"`, `"run_id":"voice-1"`} {
				if !strings.Contains(logs.String(), token) {
					t.Fatalf("missing %s in %s", token, logs.String())
				}
			}
			if strings.Contains(logs.String(), "hello") {
				t.Fatal("timing exposed text")
			}
			if tc.wantErr && !strings.Contains(logs.String(), `"success":false`) {
				t.Fatal("suppressed POST logged as success")
			}
		})
	}
}
