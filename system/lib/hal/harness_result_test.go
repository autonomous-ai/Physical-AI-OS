package hal

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestAnnounceHarnessUpdatePostsRawTextWithOwner(t *testing.T) {
	original := httpClient
	t.Cleanup(func() { httpClient = original })
	httpClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if r.URL.Path != "/voice/harness/update" || payload["kind"] != "result" || payload["text"] != "**Result**" ||
			payload["turn_id"] != "run-a" || payload["outcome"] != "completed" {
			t.Fatalf("wrong payload: %v", payload)
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"status":"queued"}`))}, nil
	})}
	if err := AnnounceHarnessUpdate(HarnessUpdateResult, "**Result**", "run-a", "completed"); err != nil {
		t.Fatal(err)
	}
}

func TestAnnounceHarnessUpdateReportsMutedSpeaker(t *testing.T) {
	original := httpClient
	t.Cleanup(func() { httpClient = original })
	httpClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"status":"suppressed"}`))}, nil
	})}
	if err := AnnounceHarnessUpdate(HarnessUpdateProgress, "working", "run-a", ""); err != ErrSpeakerMuted {
		t.Fatalf("err = %v, want ErrSpeakerMuted", err)
	}
}

func TestAnnounceHarnessUpdateCapsText(t *testing.T) {
	original := httpClient
	t.Cleanup(func() { httpClient = original })
	httpClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if n := len([]rune(payload["text"].(string))); n != harnessUpdateMaxRunes {
			t.Fatalf("text runes = %d, want %d", n, harnessUpdateMaxRunes)
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"status":"queued"}`))}, nil
	})}
	if err := AnnounceHarnessUpdate(HarnessUpdateResult, strings.Repeat("é", harnessUpdateMaxRunes+50), "run-a", ""); err != nil {
		t.Fatal(err)
	}
}
