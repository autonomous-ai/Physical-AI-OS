package hal

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestFaceEnrollPostsBodyAndDecodesResult(t *testing.T) {
	t.Cleanup(func() { faceClient = nil })
	faceClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if r.Method != "POST" || r.URL.Path != "/face/enroll" || payload["label"] != "alice" ||
			payload["image_base64"] != "aW1n" || payload["telegram_id"] != "42" {
			t.Fatalf("wrong request: %s %s %v", r.Method, r.URL.Path, payload)
		}
		if _, ok := payload["telegram_username"]; ok {
			t.Fatalf("empty telegram_username must be omitted: %v", payload)
		}
		return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(
			`{"status":"ok","label":"alice","telegram_id":"42","photo_path":"/u/alice/1.jpg","enrolled_count":2}`))}, nil
	})}
	got, err := FaceEnroll(FaceEnrollRequest{ImageBase64: "aW1n", Label: "alice", TelegramID: "42"})
	if err != nil {
		t.Fatal(err)
	}
	if got.Label != "alice" || got.PhotoPath != "/u/alice/1.jpg" || got.EnrolledCount != 2 {
		t.Fatalf("result = %+v", got)
	}
}

func TestFaceEnrollSurfacesHALDetail(t *testing.T) {
	t.Cleanup(func() { faceClient = nil })
	faceClient = &http.Client{Transport: environmentTransport(func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 400, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"detail":"no face detected"}`))}, nil
	})}
	_, err := FaceEnroll(FaceEnrollRequest{ImageBase64: "aW1n", Label: "alice"})
	if err == nil || !strings.Contains(err.Error(), "400: no face detected") {
		t.Fatalf("err = %v, want HAL detail", err)
	}
}
