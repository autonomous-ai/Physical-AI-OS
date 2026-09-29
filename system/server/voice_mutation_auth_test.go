package server

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/server/config"
	sensinghttp "go.autonomous.ai/os/system/server/sensing/delivery/http"
	"go.autonomous.ai/os/system/server/session"
)

func TestVoiceMutationRouteAuthentication(t *testing.T) {
	cfg := &config.Config{LLMAPIKey: "test-admin", SessionSecret: strings.Repeat("ab", 32)}
	cookieWriter := httptest.NewRecorder()
	cookieContext, _ := gin.CreateTestContext(cookieWriter)
	if err := session.Issue(cookieContext, cfg); err != nil {
		t.Fatal(err)
	}
	s := &Server{config: cfg, sensingHandler: &sensinghttp.SensingHandler{}}
	r := gin.New()
	s.registerVoiceMutationRoutes(r.Group("api"))
	for _, route := range []struct {
		path, body string
		accepted   int
		local      bool
	}{
		// Unknown pools do not call HAL. Invalid deletion requests do not touch files.
		{"/api/sensing/filler", `{"pool":"auth-test-nonexistent-pool"}`, http.StatusOK, true},
		{"/api/voice/file/remove", `{}`, http.StatusBadRequest, false},
	} {
		for _, tc := range []struct {
			name, remote, real, forwarded, origin, bearer string
			cookie, local                                 bool
		}{
			{name: "anonymous LAN", remote: "192.168.1.2:1234"},
			{name: "spoofed origin", remote: "192.168.1.2:1234", origin: "http://lamp-abcd.local"},
			{name: "spoofed loopback forwarding", remote: "192.168.1.2:1234", real: "127.0.0.1", forwarded: "127.0.0.1"},
			{name: "proxied LAN", remote: "127.0.0.1:1234", real: "192.168.1.2"},
			{name: "forwarded LAN", remote: "127.0.0.1:1234", forwarded: "192.168.1.2"},
			{name: "loopback", remote: "127.0.0.1:1234", local: true},
			{name: "IPv6 loopback", remote: "[::1]:1234", local: true},
			{name: "bearer", remote: "192.168.1.2:1234", bearer: "test-admin"},
			{name: "invalid bearer", remote: "192.168.1.2:1234", bearer: "wrong"},
			{name: "browser session", remote: "127.0.0.1:1234", real: "192.168.1.2", cookie: true},
		} {
			t.Run(route.path+"/"+tc.name, func(t *testing.T) {
				req := httptest.NewRequest(http.MethodPost, "http://lamp-abcd.local"+route.path, strings.NewReader(route.body))
				req.RemoteAddr = tc.remote
				req.Header.Set("Content-Type", "application/json")
				req.Header.Set("Origin", tc.origin)
				req.Header.Set("X-Real-IP", tc.real)
				req.Header.Set("X-Forwarded-For", tc.forwarded)
				if tc.bearer != "" {
					req.Header.Set("Authorization", "Bearer "+tc.bearer)
				}
				if tc.cookie {
					req.AddCookie(cookieWriter.Result().Cookies()[0])
				}
				want := http.StatusUnauthorized
				if tc.cookie || tc.bearer == cfg.LLMAPIKey || (tc.local && route.local) {
					want = route.accepted
				}
				w := httptest.NewRecorder()
				r.ServeHTTP(w, req)
				if w.Code != want {
					t.Fatalf("status %d, want %d: %s", w.Code, want, w.Body.String())
				}
			})
		}
	}
}
