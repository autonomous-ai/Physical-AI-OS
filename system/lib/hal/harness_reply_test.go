package hal

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestSpeakReplyCarriesNoHarnessCue(t *testing.T) {
	original := httpClient
	t.Cleanup(func() { httpClient = original })
	httpClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if r.URL.Path != "/voice/speak" || payload["text"] != "Original answer" || payload["realtime_feedback"] != true {
			t.Fatalf("changed reply payload: %v", payload)
		}
		if payload["harness_result"] != nil || payload["voice"] != nil || payload["provider"] != nil {
			t.Fatalf("unexpected cue or voice override: %v", payload)
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"status":"ok"}`))}, nil
	})}
	if err := SpeakReply("Original answer"); err != nil {
		t.Fatal(err)
	}
}
