package http

import (
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/server/serializers"
)

// Login responses contain short-lived authorization URLs/codes and must not be cached.
func (h *DeviceHandler) GetRuntimeLogin(c *gin.Context) {
	c.Header("Cache-Control", "no-store")
	c.JSON(http.StatusOK, serializers.ResponseSuccess(h.service.GetRuntimeLogin()))
}
func (h *DeviceHandler) StartRuntimeLogin(c *gin.Context) {
	c.Header("Cache-Control", "no-store")
	var req struct {
		Runtime  string `json:"runtime" binding:"required,max=32"`
		Provider string `json:"provider" binding:"required,max=32"`
	}
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 16384)
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("runtime and provider are required"))
		return
	}
	result, err := h.service.StartRuntimeLogin(req.Runtime, req.Provider)
	runtimeLoginResponse(c, result, err)
}
func (h *DeviceHandler) SubmitRuntimeLoginCode(c *gin.Context) {
	c.Header("Cache-Control", "no-store")
	var req struct {
		ID   string `json:"id" binding:"required,max=64"`
		Code string `json:"code" binding:"required,max=8192"`
	}
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, 16384)
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("session id and sign-in code are required"))
		return
	}
	result, err := h.service.SubmitRuntimeLoginCode(req.ID, req.Code)
	runtimeLoginResponse(c, result, err)
}
func (h *DeviceHandler) CancelRuntimeLogin(c *gin.Context) {
	c.Header("Cache-Control", "no-store")
	result, err := h.service.CancelRuntimeLogin(c.Param("id"))
	runtimeLoginResponse(c, result, err)
}
func runtimeLoginResponse(c *gin.Context, result device.RuntimeLoginSession, err error) {
	if err != nil {
		status := http.StatusBadRequest
		if errors.Is(err, device.ErrRuntimeLoginConflict) || errors.Is(err, device.ErrAgentRuntimeSwitchInProgress) {
			status = http.StatusConflict
		}
		c.JSON(status, serializers.ResponseError(err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(result))
}
