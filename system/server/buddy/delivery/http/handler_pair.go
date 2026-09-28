package http

import (
	"log/slog"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/server/serializers"
)

// PairStart issues a fresh 6-digit code. Admin-auth gated so random LAN clients
// can't trigger codes (preventing pairing-prompt spam).
func (h *BuddyHandler) PairStart(c *gin.Context) {
	code, ttl := h.service.IssuePairingCode()
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{
		"code":       code,
		"expires_in": int(ttl.Seconds()),
	}))
}

type pairConfirmRequest struct {
	Code        string `json:"code" binding:"required"`
	Name        string `json:"name"`
	Fingerprint string `json:"fingerprint"`
	OSVersion   string `json:"os_version"`
}

// RevokeSelf clears the pairing when the buddy app itself initiates unpair
// (user clicks "Revoke pairing" in the menu bar).
func (h *BuddyHandler) RevokeSelf(c *gin.Context) {
	auth := c.GetHeader("Authorization")
	if !strings.HasPrefix(auth, "Bearer ") {
		c.JSON(http.StatusUnauthorized, serializers.ResponseError("missing bearer"))
		return
	}
	token := strings.TrimPrefix(auth, "Bearer ")
	record := h.service.ValidateToken(token)
	if record == nil {
		c.JSON(http.StatusUnauthorized, serializers.ResponseError("invalid token"))
		return
	}
	slog.Info("buddy self-revoke", "component", "buddy", "id", record.BuddyID, "name", record.Name)
	if err := h.service.Unpair(); err != nil {
		c.JSON(http.StatusInternalServerError, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{"revoked": true}))
}

// PairConfirm exchanges a valid code for a long-lived token. Anonymous (the
// code itself is the credential — admin authorised its issuance).
func (h *BuddyHandler) PairConfirm(c *gin.Context) {
	var req pairConfirmRequest
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	record, err := h.service.ConfirmPairing(req.Name, req.Fingerprint, req.OSVersion, req.Code)
	if err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(gin.H{
		"token":    record.Token,
		"buddy_id": record.BuddyID,
	}))
}
