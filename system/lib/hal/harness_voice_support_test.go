package hal

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestSupportsHarnessVoiceMode(t *testing.T) {
	original := httpClient
	t.Cleanup(func() { httpClient = original })
	for _, tc := range []struct {
		name, body         string
		status             int
		supported, wantErr bool
	}{
		{"supported", `{"inputs":{"mpr121":true}}`, 200, true, false},
		{"disabled", `{"inputs":{"mpr121":false}}`, 200, false, false},
		{"old HAL", `{"device":"lamp"}`, 200, false, false},
		{"missing input", `{"inputs":{}}`, 200, false, false},
		{"wrong type", `{"inputs":{"mpr121":"true"}}`, 200, false, true},
		{"invalid", `invalid`, 200, false, true},
		{"missing endpoint", `{}`, 404, false, true},
		{"unavailable", `{}`, 503, false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			httpClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
				if r.URL.Path != "/device" || r.Method != "GET" {
					t.Fatalf("unexpected request %s %s", r.Method, r.URL)
				}
				deadline, ok := r.Context().Deadline()
				if !ok || time.Until(deadline) > time.Second {
					t.Fatal("request lacks bounded deadline")
				}
				return &http.Response{StatusCode: tc.status, Body: io.NopCloser(strings.NewReader(tc.body))}, nil
			})}
			got, err := SupportsHarnessVoiceMode(context.Background())
			if got != tc.supported || (err != nil) != tc.wantErr {
				t.Fatalf("got %v %v", got, err)
			}
		})
	}
	httpClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) { <-r.Context().Done(); return nil, r.Context().Err() })}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if supported, err := SupportsHarnessVoiceMode(ctx); supported || err == nil {
		t.Fatalf("canceled request: %v %v", supported, err)
	}
}
