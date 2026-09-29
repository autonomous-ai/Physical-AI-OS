package http

import (
	"encoding/base64"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/domain"
)

func TestEventAttachmentLimits(t *testing.T) {
	encode := func(n int) string { return base64.StdEncoding.EncodeToString(make([]byte, n)) }
	small := encode(1)
	large := encode(maxEventAttachmentBytes)
	for _, tc := range []struct {
		name  string
		req   SensingEventRequest
		valid bool
	}{
		{"empty", SensingEventRequest{}, true},
		{"mixed", SensingEventRequest{Images: []string{small}, Files: []domain.InboundFile{{Content: small}}}, true},
		{"count", SensingEventRequest{Images: []string{small, small, small, small}, Files: []domain.InboundFile{{Content: small}}}, false},
		{"boundary", SensingEventRequest{Images: []string{large, large}}, true},
		{"total", SensingEventRequest{Images: []string{large, large, small}}, false},
		{"single", SensingEventRequest{Images: []string{encode(maxEventAttachmentBytes + 1)}}, false},
		{"invalid", SensingEventRequest{Images: []string{"not base64!"}}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if err := validateEventAttachments(tc.req); (err == nil) != tc.valid {
				t.Fatalf("valid=%v err=%v", tc.valid, err)
			}
		})
	}
}

func TestOversizedSensingBodyRejectedBeforeDispatch(t *testing.T) {
	r := gin.New()
	h := &SensingHandler{}
	r.POST("/event", h.PostEvent)
	req := httptest.NewRequest("POST", "/event", strings.NewReader(`{"type":"web_chat","message":"`+strings.Repeat("a", maxSensingBodyBytes)+`"}`))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	if w.Code != 400 {
		t.Fatalf("status=%d want 400", w.Code)
	}
}
