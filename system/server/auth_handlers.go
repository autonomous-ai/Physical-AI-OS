package server

import (
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/server/serializers"
	"go.autonomous.ai/os/system/server/session"
)

// loginHandler validates the admin password and issues a session cookie.
// Any failure is a uniform 401 so the response does not leak which case fired.
func (s *Server) loginHandler(c *gin.Context) {
	var body struct {
		Password string `json:"password"`
	}
	if err := c.ShouldBindJSON(&body); err != nil || body.Password == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("password required"))
		return
	}
	now := time.Now()
	if wait := loginLimiter.retryAfter(now); wait > 0 {
		secs := int(wait.Seconds()) + 1
		c.Header("Retry-After", strconv.Itoa(secs))
		c.JSON(http.StatusTooManyRequests, serializers.ResponseError(
			fmt.Sprintf("too many failed attempts, try again in %d min", (secs+59)/60)))
		return
	}
	if err := s.deviceService.VerifyAdminPassword(body.Password); err != nil {
		loginLimiter.recordFailure(now)
		slog.Info("login rejected", "component", "auth", "error", err)
		c.JSON(http.StatusUnauthorized, serializers.ResponseError("invalid credentials"))
		return
	}
	loginLimiter.reset()
	if err := session.Issue(c, s.config); err != nil {
		slog.Error("issue session failed", "component", "auth", "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("session issue failed"))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"token": session.LatestToken(c)}))
}

// logoutHandler clears the session cookie.
func (s *Server) logoutHandler(c *gin.Context) {
	session.Clear(c)
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// loginExchangeHandler mints a session cookie for an already-authed
// adminAuthMiddleware request.
func (s *Server) loginExchangeHandler(c *gin.Context) {
	if err := session.Issue(c, s.config); err != nil {
		slog.Error("exchange session failed", "component", "auth", "error", err)
		c.JSON(http.StatusInternalServerError, serializers.ResponseError("session issue failed"))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}

// Failed logins are throttled device-wide: the default admin password is a
// short hardware suffix, so an unthrottled LAN client could try every value.
const (
	loginFailureWindow = 10 * time.Minute
	loginMaxFailures   = 10
)

var loginLimiter loginThrottle

// loginThrottle allows loginMaxFailures failed logins per sliding
// loginFailureWindow; a successful login clears the count.
type loginThrottle struct {
	mu       sync.Mutex
	failures []time.Time
}

// retryAfter returns how long logins stay blocked, or 0 when one may proceed.
func (t *loginThrottle) retryAfter(now time.Time) time.Duration {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.prune(now)
	if len(t.failures) < loginMaxFailures {
		return 0
	}
	return t.failures[0].Add(loginFailureWindow).Sub(now)
}

func (t *loginThrottle) recordFailure(now time.Time) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.prune(now)
	t.failures = append(t.failures, now)
}

func (t *loginThrottle) reset() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.failures = nil
}

// prune drops failures older than the window; callers hold mu.
func (t *loginThrottle) prune(now time.Time) {
	i := 0
	for i < len(t.failures) && now.Sub(t.failures[i]) >= loginFailureWindow {
		i++
	}
	t.failures = t.failures[i:]
}
