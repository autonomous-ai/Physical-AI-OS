package server

import (
	"log/slog"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
	"go.autonomous.ai/os/system/server/serializers"
)

// voicePreview plays a TTS preview through HAL using server-side credentials.
func (s *Server) voicePreview(c *gin.Context) {
	var body struct {
		Text     string   `json:"text"`
		Voice    string   `json:"voice"`
		Provider string   `json:"provider"`
		Speed    *float64 `json:"speed"`
		// Optional overrides — populated by the admin's Test Voice button
		// so the operator can validate pending BaseURL / APIKey edits BEFORE
		// hitting Save Changes.
		BaseURL string `json:"base_url"`
		APIKey  string `json:"api_key"`
	}
	if err := c.ShouldBindJSON(&body); err != nil || strings.TrimSpace(body.Text) == "" {
		c.JSON(http.StatusBadRequest, serializers.ResponseError("text required"))
		return
	}
	if err := domain.ValidateTTSSpeed(body.Speed); err != nil {
		c.JSON(http.StatusBadRequest, serializers.ResponseError(err.Error()))
		return
	}
	baseURL := strings.TrimSpace(body.BaseURL)
	if baseURL == "" {
		baseURL = s.config.GetTTSBaseURL()
	}
	apiKey := strings.TrimSpace(body.APIKey)
	if apiKey == "" {
		apiKey = s.config.GetTTSAPIKey()
	}
	if err := hal.SpeakPreview(body.Text, body.Voice, body.Provider, apiKey, baseURL, body.Speed); err != nil {
		slog.Warn("voice preview failed", "component", "voice", "error", err)
		c.JSON(http.StatusBadGateway, serializers.ResponseError("preview failed: "+err.Error()))
		return
	}
	c.JSON(http.StatusOK, serializers.ResponseSuccess(true))
}
