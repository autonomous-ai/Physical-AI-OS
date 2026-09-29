package server

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/server/config"
)

func TestCredentialSafeHTTPLogs(t *testing.T) {
	var logs bytes.Buffer
	router := gin.New()
	router.Use(credentialSafeLogger(&logs), credentialSafeRecovery(&logs))
	router.GET("/ok", func(c *gin.Context) {
		if c.Query("token") != "secret-query-token" {
			t.Error("logger changed the request before authentication")
		}
		c.Status(http.StatusOK)
	})
	router.GET("/panic", func(c *gin.Context) { panic(c.GetHeader("Authorization")) })
	for _, path := range []string{"/ok", "/panic"} {
		req := httptest.NewRequest(http.MethodGet, path+"?token=secret-query-token&future_secret=secret-future-key", nil)
		req.Header.Set("Authorization", "Bearer secret-panic-value")
		req.Header.Set("Cookie", "os_session=secret-cookie")
		response := httptest.NewRecorder()
		router.ServeHTTP(response, req)
		want := http.StatusOK
		if path == "/panic" {
			want = http.StatusInternalServerError
		}
		if response.Code != want {
			t.Fatalf("%s status = %d, want %d", path, response.Code, want)
		}
	}
	for _, secret := range []string{"secret-query-token", "secret-future-key", "secret-panic-value", "secret-cookie"} {
		if strings.Contains(logs.String(), secret) {
			t.Errorf("HTTP logs exposed %q", secret)
		}
	}
	for _, diagnostic := range []string{`GET "/ok"`, `GET "/panic"`, "panic recovered", "http_logging_test.go"} {
		if !strings.Contains(logs.String(), diagnostic) {
			t.Errorf("HTTP logs missing diagnostic %q", diagnostic)
		}
	}
}

type credentialTestTransport func(*http.Request) (*http.Response, error)

func (f credentialTestTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

func TestHALProxiesStripCredentialAfterAuthentication(t *testing.T) {
	for _, tc := range []struct {
		name, path, upstreamPath string
		proxy                    http.Handler
	}{
		{"hardware", "/api/hardware/camera/snapshot", "/camera/snapshot", hardwareProxy},
		{"openapi", "/openapi.json", "/openapi.json", openapiProxy},
	} {
		t.Run(tc.name, func(t *testing.T) {
			proxy := *tc.proxy.(*httputil.ReverseProxy)
			calls := 0
			proxy.Transport = credentialTestTransport(func(req *http.Request) (*http.Response, error) {
				calls++
				if strings.Contains(req.URL.RawQuery, "secret") || req.URL.Query().Has("token") {
					t.Errorf("upstream received credential query: %q", req.URL.RawQuery)
				}
				if req.URL.Path != tc.upstreamPath || req.URL.Query().Get("width") != "640" ||
					strings.Join(req.URL.Query()["tag"], ",") != "one,two" {
					t.Errorf("non-credential arguments changed: %s", req.URL)
				}
				return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: io.NopCloser(strings.NewReader("ok"))}, nil
			})
			router := gin.New()
			router.GET(tc.path, adminAuthMiddleware(&config.Config{LLMAPIKey: "secret-admin"}), gin.WrapH(&proxy))
			for _, tcAuth := range []struct {
				token string
				want  int
			}{
				{"wrong", http.StatusUnauthorized},
				{"secret-admin&token=secret-duplicate", http.StatusOK},
			} {
				response := httptest.NewRecorder()
				ctx, cancel := context.WithCancel(context.Background())
				req := httptest.NewRequest(http.MethodGet,
					tc.path+"?width=640&tag=one&tag=two&token="+tcAuth.token, nil).WithContext(ctx)
				router.ServeHTTP(response, req)
				cancel()
				if response.Code != tcAuth.want {
					t.Errorf("status = %d, want %d", response.Code, tcAuth.want)
				}
			}
			if calls != 1 {
				t.Errorf("upstream calls = %d, want 1 authenticated request", calls)
			}
		})
	}
}
