// Package http exposes the autonomous-buddy HTTP + WebSocket endpoints.
//
// Route layout (registered under /api/buddy in server.go):
//
//	POST /api/buddy/pair/start      (admin)        — issue 6-digit code, 60s TTL
//	POST /api/buddy/pair/confirm    (code in body) — exchange code for long-lived token
//	GET  /api/buddy/status          (admin)        — paired buddy summary + online flag
//	DELETE /api/buddy               (admin)        — revoke current pairing (from web UI)
//	DELETE /api/buddy/self          (bearer)       — revoke current pairing (initiated by buddy app)
//	GET  /api/buddy/ws              (bearer)       — buddy's persistent WebSocket
//	POST /api/buddy/command         (localOnly)    — dispatch a command to the buddy (OpenClaw skill calls this)
package http

import (
	"net/http"
	"sync"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/buddy"
	buddyjev "go.autonomous.ai/os/system/buddy/jev"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/server/serializers"
)

// BuddyHandler bundles the buddy-related Gin handlers.
type BuddyHandler struct {
	config          *config.Config
	service         *buddy.Service
	suggestSelector *buddyjev.Selector
	suggestGate     *sync.Mutex
}

func ProvideBuddyHandler(cfg *config.Config, svc *buddy.Service) BuddyHandler {
	return BuddyHandler{config: cfg, service: svc, suggestSelector: buddyjev.New(nil), suggestGate: &sync.Mutex{}}
}

// Status returns the pairing + connection state.
func (h *BuddyHandler) Status(c *gin.Context) {
	paired := h.service.Paired()
	if paired == nil {
		c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{
			"paired":    false,
			"connected": false,
		}))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{
		"paired":      true,
		"connected":   h.service.Connected(),
		"buddy_id":    paired.BuddyID,
		"name":        paired.Name,
		"os_version":  paired.OSVersion,
		"fingerprint": paired.Fingerprint,
		"paired_at":   paired.PairedAt,
	}))
}

// Revoke clears the current pairing (drops WS, removes on-disk record).
func (h *BuddyHandler) Revoke(c *gin.Context) {
	if err := h.service.Unpair(); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"revoked": true}))
}
