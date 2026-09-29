package server

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/server/session"
)

func TestSensingEventRequiresRemoteAuthentication(t *testing.T) {
	cfg := &config.Config{LLMAPIKey: "test-admin", SessionSecret: strings.Repeat("ab", 32)}
	cookieWriter := httptest.NewRecorder()
	cookieContext, _ := gin.CreateTestContext(cookieWriter)
	if err := session.Issue(cookieContext, cfg); err != nil {
		t.Fatal(err)
	}
	for _, event := range []string{"web_chat", "voice", "voice_command", "voice_followup", "presence", "unknown"} {
		for _, tc := range []struct {
			name, remote, real, forwarded, origin, bearer string
			cookie                                        bool
			want                                          int
		}{
			{name: "HAL", remote: "127.0.0.1:1234", want: 204},
			{name: "HAL IPv6", remote: "[::1]:1234", want: 204},
			{name: "LAN", remote: "192.168.1.2:1234", want: 401},
			{name: "forged origin", remote: "192.168.1.2:1234", origin: "http://lamp-abcd.local", want: 401},
			{name: "nginx LAN", remote: "127.0.0.1:1234", real: "192.168.1.2", want: 401},
			{name: "forwarded LAN", remote: "127.0.0.1:1234", forwarded: "192.168.1.2", want: 401},
			{name: "forged forwarding", remote: "192.168.1.2:1234", real: "127.0.0.1", want: 401},
			{name: "admin", remote: "192.168.1.2:1234", bearer: "test-admin", want: 204},
			{name: "web session", remote: "127.0.0.1:1234", real: "192.168.1.2", cookie: true, want: 204},
		} {
			t.Run(event+"/"+tc.name, func(t *testing.T) {
				r := gin.New()
				r.POST("/api/sensing/event", adminOrLoopbackAuth(cfg), func(c *gin.Context) { c.Status(http.StatusNoContent) })
				req := httptest.NewRequest("POST", "http://lamp-abcd.local/api/sensing/event", strings.NewReader(`{"type":"`+event+`","message":"hello"}`))
				req.RemoteAddr = tc.remote
				req.Header.Set("Content-Type", "application/json")
				req.Header.Set("X-Real-IP", tc.real)
				req.Header.Set("X-Forwarded-For", tc.forwarded)
				req.Header.Set("Origin", tc.origin)
				if tc.bearer != "" {
					req.Header.Set("Authorization", "Bearer "+tc.bearer)
				}
				if tc.cookie {
					req.AddCookie(cookieWriter.Result().Cookies()[0])
				}
				w := httptest.NewRecorder()
				r.ServeHTTP(w, req)
				if w.Code != tc.want {
					t.Fatalf("status %d, want %d", w.Code, tc.want)
				}
			})
		}
	}
}
