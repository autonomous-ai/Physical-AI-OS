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
// loopback (127.0.0.1:5001).
var openapiProxy = func() http.Handler {
	target, _ := url.Parse("http://127.0.0.1:5001")
	proxy := httputil.NewSingleHostReverseProxy(target)
	origDirector := proxy.Director
	proxy.Director = func(req *http.Request) {
		origDirector(req)
		req.Header.Del("X-Forwarded-For")
		req.Header.Del("X-Real-IP")
	}
	return proxy
}()

// ambientLEDGate mirrors the intent/agent paths' led_set/led_off ambient
// signals for LED writes that arrive through the hardware proxy (web UI →
// /api/hardware/* → HAL).
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
		req.URL.Path = strings.TrimPrefix(req.URL.Path, "/api/hardware")
		if req.URL.Path == "" {
			req.URL.Path = "/"
		}
		origDirector(req)
		// Stop leaking the original LAN client IP downstream: HAL's
		// same-origin/local check trusts loopback, so we present as one.
		req.Header.Del("X-Forwarded-For")
		req.Header.Del("X-Real-IP")
	}
	return proxy
}()
