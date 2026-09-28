package hal

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestSetStatusContextRequiresAcknowledgement(t *testing.T) {
	original := httpClient
	t.Cleanup(func() { httpClient = original })
	for _, tc := range []struct {
		name    string
		code    int
		body    string
		wantErr bool
	}{
		{"success", 200, `{"status":"ok","effect":"solid"}`, false},
		{"not ready", 503, `{"detail":"LED not available"}`, true},
		{"invalid response", 200, `invalid`, true},
		{"missing acknowledgement", 200, `{}`, true},
		{"rejected", 200, `{"status":"error"}`, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			httpClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
				body, _ := io.ReadAll(r.Body)
				if r.Method != "POST" || r.URL.Path != "/led/status" || string(body) != `{"state":"setup"}` {
					t.Fatalf("unexpected request: %s %s %s", r.Method, r.URL, body)
				}
				return &http.Response{StatusCode: tc.code, Body: io.NopCloser(strings.NewReader(tc.body))}, nil
			})}
			if err := SetStatusContext(context.Background(), "setup"); (err != nil) != tc.wantErr {
				t.Fatalf("unexpected error: %v", err)
			}
		})
	}
}

func TestSetupRequestsPropagateCancellation(t *testing.T) {
	original := httpClient
	t.Cleanup(func() { httpClient = original })
	httpClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
		<-r.Context().Done()
		return nil, r.Context().Err()
	})}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := SetStatusContext(ctx, "setup"); !errors.Is(err, context.Canceled) {
		t.Fatalf("paint cancellation: %v", err)
	}
	if _, err := GetHealthContext(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("health cancellation: %v", err)
	}
}
