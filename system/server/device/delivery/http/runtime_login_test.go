package http

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
)

func TestRuntimeLoginRejectsMalformedRequests(t *testing.T) {
	for _, tc := range []struct {
		body string
		code bool
	}{{`{}`, false}, {`{"runtime":"claudecode"}`, false}, {`{"id":"x"}`, true}, {`{"id":"x","code":"` + strings.Repeat("x", 17000) + `"}`, true}} {
		recorder := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(recorder)
		c.Request = httptest.NewRequest(http.MethodPost, "/api/device/runtime-login", strings.NewReader(tc.body))
		c.Request.Header.Set("Content-Type", "application/json")
		handler := &DeviceHandler{}
		if tc.code {
			handler.SubmitRuntimeLoginCode(c)
		} else {
			handler.StartRuntimeLogin(c)
		}
		if recorder.Code != http.StatusBadRequest {
			t.Fatalf("status %d", recorder.Code)
		}
		if recorder.Header().Get("Cache-Control") != "no-store" {
			t.Fatal("login response can be cached")
		}
	}
}
