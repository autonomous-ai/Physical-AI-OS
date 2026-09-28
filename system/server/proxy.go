package server

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"

	"github.com/gin-gonic/gin"
)

// hardwareProxy is a wildcard reverse proxy from /api/hardware/* to HAL on
// loopback (127.0.0.1:5001). It exists so the web UI never touches /hw/*
// directly — adminAuthMiddleware gates the bearer here, and the upstream
// HAL call is loopback so its local_only_middleware lets it through.
//
// MJPEG streams (/api/hardware/camera/stream) work because
// httputil.ReverseProxy disables response buffering for chunked / multipart
// content out of the box. Long-running endpoints reuse the default 300s
// proxy timeout configured at the http.Server level.
// openapiProxy serves the in-iframe Swagger UI's `/openapi.json` fetch by
// forwarding straight to HAL on loopback. Path stays as-is — FastAPI
// generates the spec at /openapi.json on HAL, no rewrite needed.
var openapiProxy = func() http.Handler {
	target, _ := url.Parse("http://127.0.0.1:5001")
	proxy := httputil.NewSingleHostReverseProxy(target)
	origDirector := proxy.Director
	proxy.Director = func(req *http.Request) {
		origDirector(req)
		stripProxyCredential(req)
		req.Header.Del("X-Forwarded-For")
		req.Header.Del("X-Real-IP")
	}
	return proxy
}()

// ambientLEDGate mirrors the intent/agent paths' led_set/led_off ambient
// signals for LED writes that arrive through the hardware proxy (web UI →
// /api/hardware/* → HAL). Those writes never pass through the intent matcher
// or agent tool events, so without this ambient's ledLocked stays false and
// its breathing loop resumes ~60s after the last interaction, trampling the
// web-set color/effect. Transient writes (ambient itself, Buddy, status
// overlays) don't lock — same rule as HAL's user-state save.
func (s *Server) ambientLEDGate() gin.HandlerFunc {
	return func(c *gin.Context) {
		if s.ambientService != nil && c.Request.Method == http.MethodPost {
			switch c.Param("path") {
			case "/led/solid", "/led/effect", "/scene":
				if !transientBody(c) {
					s.ambientService.LockLED()
				}
			case "/led/paint":
				s.ambientService.LockLED()
			case "/led/off":
				if !transientBody(c) {
					s.ambientService.UnlockLED()
				}
			case "/scene/off":
				s.ambientService.UnlockLED()
			}
		}
		c.Next()
	}
}

// transientBody peeks at the JSON body for `"transient": true`, restoring the
// body for the proxy. A missing/unreadable body counts as non-transient.
func transientBody(c *gin.Context) bool {
	if c.Request.Body == nil {
		return false
	}
	body, err := io.ReadAll(io.LimitReader(c.Request.Body, 1<<20))
	if err != nil {
		c.Request.Body = io.NopCloser(bytes.NewReader(nil))
		return false
	}
	c.Request.Body = io.NopCloser(bytes.NewReader(body))
	var probe struct {
		Transient bool `json:"transient"`
	}
	_ = json.Unmarshal(body, &probe)
	return probe.Transient
}

var hardwareProxy = func() http.Handler {
	target, _ := url.Parse("http://127.0.0.1:5001")
	proxy := httputil.NewSingleHostReverseProxy(target)
	origDirector := proxy.Director
	proxy.Director = func(req *http.Request) {
		// Gin's wildcard match leaves /api/hardware/<path> in req.URL.Path.
		// Strip the prefix so HAL sees its original path.
		req.URL.Path = strings.TrimPrefix(req.URL.Path, "/api/hardware")
		if req.URL.Path == "" {
			req.URL.Path = "/"
		}
		origDirector(req)
		stripProxyCredential(req)
		// Stop leaking the original LAN client IP downstream: HAL's
		// same-origin/local check trusts loopback, so we present as one.
		req.Header.Del("X-Forwarded-For")
		req.Header.Del("X-Real-IP")
	}
	return proxy
}()

// Authentication has already run in Go. HAL must never receive the browser's
// legacy admin credential, including through its access log query string.
func stripProxyCredential(req *http.Request) {
	query := req.URL.Query()
	if _, present := query["token"]; present {
		query.Del("token")
		req.URL.RawQuery = query.Encode()
	}
}
