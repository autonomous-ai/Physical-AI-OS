// Package http exposes the autonomous-buddy HTTP + WebSocket endpoints.
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
