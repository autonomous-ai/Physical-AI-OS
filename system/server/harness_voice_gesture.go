package server

import (
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/server/serializers"
)

// handleHarnessVoiceGesture is called by HAL's action worker, never by the
// input poller.
func (s *Server) handleHarnessVoiceGesture(c *gin.Context) {
	c.Header("Cache-Control", "no-store")
	var req struct {
		GestureID string `json:"gestureId" binding:"required,uuid"`
		Action    string `json:"action" binding:"omitempty,oneof=toggle disable"`
	}
	if err := bindHarnessVoiceJSON(c, &req); err != nil {
		return
	}
	if s.harnessVoice == nil {
		writeHarnessGestureError(c, http.StatusServiceUnavailable, "harness_offline", "Harness voice service unavailable")
		return
	}
	var state harness.VoiceModeState
	var err error
	if req.Action == "disable" {
		// A delayed hold or HTTP retry must never enable voice mode again.
		state, err = s.harnessVoice.SetMode(c.Request.Context(), false)
	} else {
		state, err = s.harnessVoice.ToggleGesture(c.Request.Context(), req.GestureID)
	}
	if err != nil {
		code := "focus_unavailable"
		var actionError *harness.VoiceGestureError
		if errors.As(err, &actionError) {
			code = actionError.Code
		}
		writeHarnessGestureError(c, http.StatusConflict, code, err.Error())
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(state))
}
func writeHarnessGestureError(c *gin.Context, status int, code, message string) {
	c.JSON(status, gin.H{"status": 0, "data": gin.H{"code": code}, "message": message})
}

func (s *Server) handleHarnessVoiceFocusGesture(c *gin.Context) {
	c.Header("Cache-Control", "no-store")
	var req struct {
		GestureID  string `json:"gestureId" binding:"required,uuid"`
		Direction  string `json:"direction" binding:"required,oneof=next previous"`
		Generation *int64 `json:"generation" binding:"required,gte=0"`
	}
	if err := bindHarnessVoiceJSON(c, &req); err != nil {
		return
	}
	if s.harnessVoice == nil {
		writeHarnessGestureError(c, http.StatusServiceUnavailable, "harness_offline", "Harness voice service unavailable")
		return
	}
	state, err := s.harnessVoice.StepFocus(c.Request.Context(), req.GestureID, req.Direction, uint64(*req.Generation))
	if err != nil {
		code := "focus_unavailable"
		var actionError *harness.VoiceGestureError
		if errors.As(err, &actionError) {
			code = actionError.Code
		}
		writeHarnessGestureError(c, http.StatusConflict, code, err.Error())
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(state))
}
