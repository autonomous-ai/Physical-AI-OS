package server

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/server/config"
)

type voicePreviewTransport func(*http.Request) (*http.Response, error)

func (f voicePreviewTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestVoicePreviewSpeed(t *testing.T) {
	original := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = original })
	for _, tc := range []struct {
		name, speed string
		want        *float64
		status      int
	}{
		{"omitted", "", nil, http.StatusOK},
		{"null", `,"speed":null`, nil, http.StatusOK},
		{"draft", `,"speed":1.75`, speedPointer(1.75), http.StatusOK},
		{"minimum", `,"speed":0.25`, speedPointer(0.25), http.StatusOK},
		{"maximum", `,"speed":4`, speedPointer(4), http.StatusOK},
		{"zero", `,"speed":0`, nil, http.StatusBadRequest},
		{"too slow", `,"speed":0.24`, nil, http.StatusBadRequest},
		{"too fast", `,"speed":4.01`, nil, http.StatusBadRequest},
		{"string", `,"speed":"1.5"`, nil, http.StatusBadRequest},
	} {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			http.DefaultTransport = voicePreviewTransport(func(r *http.Request) (*http.Response, error) {
				calls++
				if r.Method != http.MethodPost || r.URL.Path != "/voice/speak" {
					t.Fatalf("unexpected HAL request: %s %s", r.Method, r.URL)
				}
				var payload map[string]any
				if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
					t.Fatal(err)
				}
				got, present := payload["speed"]
				if tc.want == nil && present || tc.want != nil && got != *tc.want {
					t.Errorf("HAL speed = %v, present %v; want %v", got, present, tc.want)
				}
				return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(`{}`))}, nil
			})
			cfg := &config.Config{TTSSpeed: speedPointer(1.2)}
			s := &Server{config: cfg}
			recorder := httptest.NewRecorder()
			ctx, _ := gin.CreateTestContext(recorder)
			ctx.Request = httptest.NewRequest(http.MethodPost, "/api/voice/preview", strings.NewReader(`{"text":"Hello"`+tc.speed+`}`))
			ctx.Request.Header.Set("Content-Type", "application/json")
			s.voicePreview(ctx)
			if recorder.Code != tc.status {
				t.Fatalf("status = %d, want %d: %s", recorder.Code, tc.status, recorder.Body.String())
			}
			if tc.status == http.StatusOK && calls != 1 || tc.status != http.StatusOK && calls != 0 {
				t.Errorf("unexpected HAL call count: %d", calls)
			}
			if cfg.GetTTSSpeed() != 1.2 {
				t.Errorf("preview modified saved speed: %v", cfg.GetTTSSpeed())
			}
		})
	}
}

func speedPointer(value float64) *float64 { return &value }
