package hal

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestVoiceRecordEnrollPostsBodyAndDecodesMeta(t *testing.T) {
	t.Cleanup(func() { voiceClient = nil })
	voiceClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if r.Method != "POST" || r.URL.Path != "/speaker/record-enroll" || payload["name"] != "alice" ||
			payload["duration_sec"] != float64(15) {
			t.Fatalf("wrong request: %s %s %v", r.Method, r.URL.Path, payload)
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(
			`{"status":"ok","meta":{"name":"alice","display_name":"Alice","num_samples":2,"embedding_dim":192,` +
				`"enrollment_sources":["web"],"sample_files":["sample_web_1.wav"]}}`))}, nil
	})}
	got, err := VoiceRecordEnroll(VoiceRecordEnrollRequest{Name: "alice", DurationSec: 15})
	if err != nil {
		t.Fatal(err)
	}
	if got.Meta.Name != "alice" || got.Meta.DisplayName != "Alice" || got.Meta.NumSamples != 2 || len(got.Meta.EnrollmentSources) != 1 {
		t.Fatalf("result = %+v", got)
	}
}

func TestVoiceRecordEnrollSurfacesHALDetail(t *testing.T) {
	t.Cleanup(func() { voiceClient = nil })
	voiceClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 409, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(
			`{"detail":"Privacy switch is on -- microphone recording is blocked"}`))}, nil
	})}
	_, err := VoiceRecordEnroll(VoiceRecordEnrollRequest{Name: "alice", DurationSec: 15})
	if err == nil || !strings.Contains(err.Error(), "409: Privacy switch is on") {
		t.Fatalf("err = %v, want HAL detail", err)
	}
}

func TestVoiceRemovePostsName(t *testing.T) {
	t.Cleanup(func() { voiceClient = nil })
	voiceClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
		var payload map[string]any
		_ = json.NewDecoder(r.Body).Decode(&payload)
		if r.URL.Path != "/speaker/remove" || payload["name"] != "alice" {
			t.Fatalf("wrong request: %s %v", r.URL.Path, payload)
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(
			`{"status":"ok","name":"alice","removed":true}`))}, nil
	})}
	got, err := VoiceRemove("alice")
	if err != nil || !got.Removed || got.Name != "alice" {
		t.Fatalf("got %+v, err %v", got, err)
	}
}
