package server

import (
	"crypto/subtle"
	"net"
	"net/http"
	"regexp"
	"strings"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/server/session"
)

func goSameOrigin(header, host string) bool {
	if header == "" || host == "" {
		return false
	}
	h := strings.TrimPrefix(strings.TrimPrefix(strings.TrimSpace(header), "https://"), "http://")
	h = strings.SplitN(h, "/", 2)[0]
	return h == host
}

// siblingDeviceHost matches an Autonomous device's mDNS hostname:
// `<device_type>-<4 hex>.local` (see GetDeviceMac / setup.sh).
var siblingDeviceHost = regexp.MustCompile(`^[a-z0-9]+-[0-9a-f]{4}\.local$`)

// isAllowedOrigin returns true for same-host origins and approved external
// domains (autonomous.ai subdomains for parent-app embedding, sibling
// <device_type>-XXXX.local devices on the same LAN).
func isAllowedOrigin(origin, requestHost string) bool {
	if origin == "" {
		return false
	}
	h := strings.TrimPrefix(strings.TrimPrefix(strings.TrimSpace(origin), "https://"), "http://")
	h = strings.SplitN(h, "/", 2)[0]
	if i := strings.IndexByte(h, ':'); i >= 0 {
		h = h[:i]
	}
	reqHost := requestHost
	if i := strings.IndexByte(reqHost, ':'); i >= 0 {
		reqHost = reqHost[:i]
	}
	if h == reqHost {
		return true
	}
	if h == "autonomous.ai" || strings.HasSuffix(h, ".autonomous.ai") {
		return true
	}
	if siblingDeviceHost.MatchString(h) {
		return true
	}
	if h == "huggingface.co" || strings.HasSuffix(h, ".huggingface.co") ||
		strings.HasSuffix(h, ".hf.space") {
		return true
	}
	if h == "localhost" || h == "127.0.0.1" {
		return true
	}
	return false
}

func isLoopbackHost(host string) bool {
	host = strings.Trim(host, "[]")
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

func firstForwardedFor(v string) string {
	if v == "" {
		return ""
	}
	return strings.TrimSpace(strings.Split(v, ",")[0])
}

func hostOnly(addr string) string {
	if h, _, err := net.SplitHostPort(addr); err == nil {
		return h
	}
	return strings.Trim(addr, "[]")
}

// adminOrLoopbackAuth gates an endpoint with a hybrid policy: a
// strict-loopback origin (no nginx proxy headers) bypasses auth entirely;
// everything else must pass adminAuthMiddleware.
func adminOrLoopbackAuth(cfg *config.Config) gin.HandlerFunc {
	admin := adminAuthMiddleware(cfg)
	return func(c *gin.Context) {
		remoteHost := hostOnly(c.Request.RemoteAddr)
		xff := firstForwardedFor(c.GetHeader("X-Forwarded-For"))
		realIP := strings.TrimSpace(c.GetHeader("X-Real-IP"))
		if isLoopbackHost(remoteHost) &&
			(xff == "" || isLoopbackHost(xff)) &&
			(realIP == "" || isLoopbackHost(realIP)) {
			c.Next()
			return
		}
		admin(c)
	}
}

// localOnlyMiddleware blocks any request whose real client IP is not
// loopback, checking X-Forwarded-For/X-Real-IP since nginx peers are loopback.
func localOnlyMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		remoteHost := hostOnly(c.Request.RemoteAddr)
		xff := firstForwardedFor(c.GetHeader("X-Forwarded-For"))
		realIP := strings.TrimSpace(c.GetHeader("X-Real-IP"))

		if !isLoopbackHost(remoteHost) ||
			(xff != "" && !isLoopbackHost(xff)) ||
			(realIP != "" && !isLoopbackHost(realIP)) {
			c.JSON(http.StatusForbidden, serializers.ResponseError("local-only endpoint"))
			c.Abort()
			return
		}
		c.Next()
	}
}

// setupOrAdminMiddleware leaves POST /api/device/setup open until
// SetUpCompleted, then requires adminAuthMiddleware (audit go F8a).
func setupOrAdminMiddleware(cfg *config.Config) gin.HandlerFunc {
	authMW := adminAuthMiddleware(cfg)
	return func(c *gin.Context) {
		if !cfg.SetUpCompleted {
			c.Next()
			return
		}
		authMW(c)
	}
}

// apOnlyMiddleware admits requests whose source IP sits inside the AP's own
// DHCP subnet AND whose Host header matches the AP's static IP.
// The source-IP check is the security gate; nginx overwrites X-Real-IP.
func apOnlyMiddleware() gin.HandlerFunc {
	_, apSubnet, _ := net.ParseCIDR("192.168.100.0/24")
	const apStaticHost = "192.168.100.1"
	return func(c *gin.Context) {
		host := c.Request.Host
		if h, _, err := net.SplitHostPort(host); err == nil {
			host = h
		}
		if host != apStaticHost {
			c.JSON(http.StatusForbidden, serializers.ResponseError("AP portal only"))
			c.Abort()
			return
		}
		clientIP := strings.TrimSpace(c.GetHeader("X-Real-IP"))
		if clientIP == "" {
			clientIP = strings.TrimSpace(strings.SplitN(c.GetHeader("X-Forwarded-For"), ",", 2)[0])
		}
		if clientIP == "" {
			remoteHost, _, _ := net.SplitHostPort(c.Request.RemoteAddr)
			clientIP = remoteHost
		}
		ip := net.ParseIP(clientIP)
		if ip == nil || !apSubnet.Contains(ip) {
			c.JSON(http.StatusForbidden, serializers.ResponseError("AP portal only"))
			c.Abort()
			return
		}
		c.Next()
	}
}

// adminAuthMiddleware admits a valid os_session cookie, a session token, or a
// Bearer/?token= matching cfg.LLMAPIKey (read per request, constant-time
// compare). Fails closed with 503 when no key is configured.
func adminAuthMiddleware(cfg *config.Config) gin.HandlerFunc {
	return func(c *gin.Context) {
		if session.HasValid(c, cfg) {
			c.Next()
			return
		}
		bearer := strings.TrimSpace(strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer "))
		if bearer != "" && session.VerifyToken(bearer, cfg) {
			c.Next()
			return
		}
		expected := cfg.LLMAPIKey
		if expected == "" {
			c.JSON(http.StatusServiceUnavailable, serializers.ResponseError("admin auth not configured"))
			c.Abort()
			return
		}
		got := strings.TrimSpace(strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer "))
		if got == "" {
			got = strings.TrimSpace(c.Query("token"))
		}
		if got == "" || subtle.ConstantTimeCompare([]byte(got), []byte(expected)) != 1 {
			c.JSON(http.StatusUnauthorized, serializers.ResponseError("unauthorized"))
			c.Abort()
			return
		}
		c.Next()
	}
}

func corsMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		origin := c.GetHeader("Origin")
		if isAllowedOrigin(origin, c.Request.Host) {
			c.Header("Access-Control-Allow-Origin", origin)
			c.Header("Vary", "Origin")
			c.Header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
			c.Header("Access-Control-Allow-Headers", "Origin, Content-Type, Accept, Authorization, X-Requested-With")
			c.Header("Access-Control-Allow-Credentials", "true")
		}
		if c.Request.Method == "OPTIONS" {
			c.AbortWithStatus(http.StatusNoContent)
			return
		}
		c.Next()
	}
}
