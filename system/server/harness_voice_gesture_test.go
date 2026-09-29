package server

import (
	"context"
	"fmt"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/harness"
)

func TestHarnessVoiceGestureLocalAuthAndDeduplication(t *testing.T) {
	voice := harness.NewVoiceController(&voiceRouteTransport{}, harness.VoiceCallbacks{SupportsMode: func(context.Context) (bool, error) { return true, nil }})
	s := &Server{harnessVoice: voice}
	router := gin.New()
	s.registerHarnessVoiceRoutes(router.Group("/api/harness"))
	const payload = `{"gestureId":"07c61377-971e-47a9-8742-e0f72d441955"}`
	call := func(address, forwarded, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest("POST", "/api/harness/voice-mode/gesture", strings.NewReader(body))
		req.RemoteAddr = address
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Forwarded-For", forwarded)
		out := httptest.NewRecorder()
		router.ServeHTTP(out, req)
		return out
	}
	for _, tc := range []struct{ address, forwarded string }{
		{"192.168.1.2:50", ""}, {"127.0.0.1:50", "192.168.1.2"},
	} {
		if out := call(tc.address, tc.forwarded, payload); out.Code != 403 {
			t.Fatalf("remote allowed: %d", out.Code)
		}
	}
	for _, body := range []string{`{}`, `{"gestureId":""}`, `{"gestureId":"not-a-uuid"}`, `{"gestureId":"07c61377-971e-47a9-8742-e0f72d441955","action":"enable"}`} {
		if out := call("127.0.0.1:50", "", body); out.Code != 400 {
			t.Fatalf("invalid ID allowed: %d", out.Code)
		}
	}
	for range 2 {
		out := call("127.0.0.1:50", "", payload)
		if out.Code != 200 || !voice.State().Enabled || !strings.Contains(out.Body.String(), `"agentName":"Mike"`) {
			t.Fatalf("gesture: %d %s", out.Code, out.Body.String())
		}
	}
	_, _ = voice.SetMode(context.Background(), false)
	out := call("127.0.0.1:50", "", payload)
	if out.Code != 200 || voice.State().Enabled {
		t.Fatal("old HTTP retry toggled after explicit off")
	}
}

func TestHarnessVoiceGestureDisableIsIdempotentOffline(t *testing.T) {
	transport, err := harness.NewService(t.TempDir(), harness.Callbacks{})
	if err != nil {
		t.Fatal(err)
	}
	voice := harness.NewVoiceController(transport, harness.VoiceCallbacks{SupportsMode: func(context.Context) (bool, error) { return true, nil }})
	s := &Server{harnessVoice: voice}
	router := gin.New()
	s.registerHarnessVoiceRoutes(router.Group("/api/harness"))
	_, _ = voice.SetMode(context.Background(), true)
	const payload = `{"gestureId":"07c61377-971e-47a9-8742-e0f72d441955","action":"disable"}`
	var generation uint64
	for attempt := range 3 {
		req := httptest.NewRequest("POST", "/api/harness/voice-mode/gesture", strings.NewReader(payload))
		req.RemoteAddr = "127.0.0.1:50"
		req.Header.Set("Content-Type", "application/json")
		out := httptest.NewRecorder()
		router.ServeHTTP(out, req)
		state := voice.State()
		if out.Code != 200 || state.Enabled || !strings.Contains(out.Body.String(), `"enabled":false`) {
			t.Fatalf("disable attempt %d: %d %s", attempt, out.Code, out.Body.String())
		}
		if attempt > 0 && state.Generation != generation {
			t.Fatal("repeated disable changed capture generation")
		}
		generation = state.Generation
	}
}

func TestHarnessVoiceFocusGestureValidationAndAuth(t *testing.T) {
	voice := harness.NewVoiceController(&voiceRouteTransport{}, harness.VoiceCallbacks{SupportsMode: func(context.Context) (bool, error) { return true, nil }})
	_, _ = voice.SetMode(context.Background(), true)
	s := &Server{harnessVoice: voice}
	router := gin.New()
	s.registerHarnessVoiceRoutes(router.Group("/api/harness"))
	call := func(address, forwarded, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest("POST", "/api/harness/voice-mode/focus", strings.NewReader(body))
		req.RemoteAddr = address
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Forwarded-For", forwarded)
		out := httptest.NewRecorder()
		router.ServeHTTP(out, req)
		return out
	}
	valid := fmt.Sprintf(`{"gestureId":"07c61377-971e-47a9-8742-e0f72d441955","direction":"next","generation":%d}`, voice.State().Generation)
	for _, address := range []string{"192.168.1.2:50", "127.0.0.1:50"} {
		if out := call(address, "192.168.1.2", valid); out.Code != 403 {
			t.Fatalf("remote allowed: %d", out.Code)
		}
	}
	for _, body := range []string{
		`{}`,
		`{"gestureId":"07c61377-971e-47a9-8742-e0f72d441955","direction":"next"}`,
		`{"gestureId":"07c61377-971e-47a9-8742-e0f72d441955","direction":"next","generation":-1}`,
		`{"gestureId":"07c61377-971e-47a9-8742-e0f72d441955","direction":"left","generation":1}`,
		`{"gestureId":"bad","direction":"next","generation":1}`,
	} {
		if out := call("127.0.0.1:50", "", body); out.Code != 400 {
			t.Fatalf("invalid body allowed: %d %s", out.Code, out.Body.String())
		}
	}
	out := call("127.0.0.1:50", "", valid)
	if out.Code != 409 || !strings.Contains(out.Body.String(), `"code":"unsupported"`) {
		t.Fatalf("unsupported CLI: %d %s", out.Code, out.Body.String())
	}
}
