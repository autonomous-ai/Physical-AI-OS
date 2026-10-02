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
	"testing/iotest"

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

// A proxied stream that breaks mid-body is a disconnect, not a crash: the
// request is still logged, but without a panic stack trace.
func TestProxiedStreamAbortIsNotLoggedAsPanic(t *testing.T) {
	var logs bytes.Buffer
	proxy := *hardwareProxy.(*httputil.ReverseProxy)
	proxy.Transport = credentialTestTransport(func(*http.Request) (*http.Response, error) {
		body := io.MultiReader(strings.NewReader("data: 1\n\n"), iotest.ErrReader(io.ErrUnexpectedEOF))
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: io.NopCloser(body)}, nil
	})
	router := gin.New()
	router.Use(credentialSafeLogger(&logs), credentialSafeRecovery(&logs))
	router.GET("/api/hardware/*path", gin.WrapH(&proxy))

	response := httptest.NewRecorder()
	// The proxy only panics on a broken body when it sees it runs under an
	// http.Server, which is what happens on the device.
	ctx, cancel := context.WithCancel(context.WithValue(context.Background(), http.ServerContextKey, &http.Server{}))
	defer cancel()
	router.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/api/hardware/voice/mic-level", nil).WithContext(ctx))

	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), "data: 1") {
		t.Errorf("response = %d %q, want the bytes streamed before the abort", response.Code, response.Body.String())
	}
	if !strings.Contains(logs.String(), `| 200 |`) || !strings.Contains(logs.String(), `GET "/api/hardware/voice/mic-level"`) {
		t.Errorf("aborted stream missing from access log: %q", logs.String())
	}
	if strings.Contains(logs.String(), "panic recovered") {
		t.Errorf("aborted stream logged as a panic:\n%s", logs.String())
	}
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
