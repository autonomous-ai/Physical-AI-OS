package server

import (
	"log/slog"
	"net/http"

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
	if err := s.deviceService.VerifyAdminPassword(body.Password); err != nil {
		slog.Info("login rejected", "component", "auth", "error", err)
		c.JSON(http.StatusUnauthorized, serializers.ResponseError("invalid credentials"))
		return
	}
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
