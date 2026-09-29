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
// `<device_type>-<4 hex>.local` (see GetDeviceMac / setup.sh). Device-agnostic by
// design — trusts any device class on the LAN, e.g. lamp-a1b2.local,
// intern-3c4d.local — not just one device type.
var siblingDeviceHost = regexp.MustCompile(`^[a-z0-9]+-[0-9a-f]{4}\.local$`)

// isAllowedOrigin returns true for same-host origins and approved external
// domains (autonomous.ai subdomains for parent-app embedding, sibling
// <device_type>-XXXX.local devices on the same LAN). Same-host wins for any IP or
// .local hostname the device itself is reached on.
func isAllowedOrigin(origin, requestHost string) bool {
	if origin == "" {
		return false
	}
	h := strings.TrimPrefix(strings.TrimPrefix(strings.TrimSpace(origin), "https://"), "http://")
	h = strings.SplitN(h, "/", 2)[0]
	// Port-insensitive comparison so :80/:5000 dev variants match the canonical host.
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
	// Autonomous parent app (www.autonomous.ai + any subdomain). Driven by
	// product flows that embed device screens or hit device APIs from the
	// cloud dashboard; mixed-content rules still apply at the browser layer
	// (HTTPS parent → HTTP device fails before CORS) — this just stops the
	// device from rejecting the request when the parent reaches it over the
	// LAN through a Tailscale/HTTPS-proxy fronting layer.
	if h == "autonomous.ai" || strings.HasSuffix(h, ".autonomous.ai") {
		return true
	}
	// Sibling Autonomous devices on the same LAN (mDNS hostname
	// `<device_type>-XXXX.local`, e.g. lamp-a1b2.local / intern-3c4d.local).
	if siblingDeviceHost.MatchString(h) {
		return true
	}
	// HuggingFace Spaces (community apps deployed as static JS apps).
	if h == "huggingface.co" || strings.HasSuffix(h, ".huggingface.co") ||
		strings.HasSuffix(h, ".hf.space") {
		return true
	}
	// Localhost (dev / local community apps served via python -m http.server).
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

// adminOrLoopbackAuth gates an endpoint with a hybrid policy: a strict-loopback
// origin (no nginx proxy headers) bypasses auth entirely; everything else must
// pass adminAuthMiddleware. It permits physical-device/internal operations such
// as /api/system/factory-reset and /api/guard while web calls from the LAN
// still need admin credentials.
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

// localOnlyMiddleware blocks any request whose real client IP is not loopback.
// Checks RemoteAddr, X-Forwarded-For, and X-Real-IP so nginx-proxied LAN
// requests are still rejected even though the TCP peer is always 127.0.0.1.
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

// adminAuthMiddleware admits a request when any of these holds:
//   - Authorization: Bearer <llm_api_key> matches cfg.LLMAPIKey (scripts, curl)
//   - os_session cookie validates under cfg.SessionSecret (browser, post-login)
//   - ?token=<llm_api_key> query param matches (legacy <img>/<a>/EventSource —
//     still needed for cases where the browser can't set headers AND cookies
//     can't ride along, e.g. cross-tab popups)
//
// Reading the expected token from cfg.LLMAPIKey at request time means a
// PUT /api/device/config rotation takes effect without a restart. Constant-time
// compare on the bearer path keeps timing channels closed. Empty configured key
// AND empty session secret both fail closed (503 admin auth not configured).
//
// setupOrAdminMiddleware gates POST /api/device/setup with a hybrid policy:
//   - SetUpCompleted == false → open (fresh device; no admin exists yet, can't
//     require auth or first-boot is impossible)
//   - SetUpCompleted == true  → adminAuthMiddleware (re-setup is a config
//     rewrite, treat it as an admin op — Bearer llm_api_key or session cookie)
//
// Replaces the old setupOnlyMiddleware (audit go F8a) so the web `#force`
// re-setup path still works for operators who own the admin credential, while
// keeping the original audit goal (no unauthed re-setup post-provision).
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
// DHCP subnet AND whose Host header matches the AP's static IP. Both checks
// are required:
//
//   - Source-IP check is the security gate — a LAN attacker sending
//     `-H "Host: 192.168.100.1"` still carries their real LAN IP (nginx
//     strips inbound X-Real-IP and sets its own from the real client), and
//     that IP is outside the AP subnet, so the bypass is refused.
//   - Host check makes the intent explicit and rejects accidental
//     cross-interface calls that might land here via a misconfigured proxy.
//
// Used only for POST /api/device/wifi-provision — the fast-path re-Wi-Fi
// endpoint served from the provisioning AP portal. Physical presence on the
// hotspot is the trust signal; without a valid home Wi-Fi the operator has
// no way to auth via the LAN IP, and asking them to know llm_api_key on the
// setup screen is a UX dead-end.
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

func adminAuthMiddleware(cfg *config.Config) gin.HandlerFunc {
	return func(c *gin.Context) {
		// Session cookie path (browser, post-login). Cookies auto-attach so
		// this covers <img>, <a>, EventSource and any same-site fetch.
		if session.HasValid(c, cfg) {
			c.Next()
			return
		}
		// Bearer as session token (cross-origin apps that got token from login).
		bearer := strings.TrimSpace(strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer "))
		if bearer != "" && session.VerifyToken(bearer, cfg) {
			c.Next()
			return
		}
		expected := cfg.LLMAPIKey
		if expected == "" {
			// No bearer configured AND no valid session → can't admit anyone.
			c.JSON(http.StatusServiceUnavailable, serializers.ResponseError("admin auth not configured"))
			c.Abort()
			return
		}
		// Bearer header (preferred) or ?token= query (legacy fallback for
		// places where headers and cookies both can't ride: cross-tab popups,
		// download links rendered into srcdoc iframes).
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
			// Required for the patched fetch (credentials: "include") to receive
			// the session cookie on cross-origin responses from autonomous.ai
			// or sibling <device_type>-*.local devices.
			c.Header("Access-Control-Allow-Credentials", "true")
		}
		if c.Request.Method == "OPTIONS" {
			c.AbortWithStatus(http.StatusNoContent)
			return
		}
		c.Next()
	}
}
