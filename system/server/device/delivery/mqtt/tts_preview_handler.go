package mqtthandler

import (
	"encoding/json"
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/hal"
)

// handleTTSPreview plays a one-shot TTS preview on the lamp without
// persisting any config change. Mirrors the HTTP /api/voice/preview flow:
// API key + base URL are read server-side from config so the BFF never
// has to ship credentials over MQTT. Provider/voice/language overrides
// are optional — empty fields make HAL fall back to current config.
// Speed applies only to this preview, without changing saved/runtime defaults.
func (h *DeviceMQTTHandler) handleTTSPreview(env domain.MQTTDataCommand) error {
	var req domain.MQTTTTSPreviewData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		slog.Error("tts.preview: invalid payload", "component", "mqtt", "error", err)
		return h.publishDataResult(domain.KindTTSPreview, "failure", "invalid JSON payload", nil)
	}

	if strings.TrimSpace(req.Text) == "" {
		slog.Warn("tts.preview: missing text", "component", "mqtt")
		return h.publishDataResult(domain.KindTTSPreview, "failure", "text is required", nil)
	}

	if err := domain.ValidateTTSSpeed(req.Speed); err != nil {
		return h.publishDataResult(domain.KindTTSPreview, "failure", err.Error(), nil)
	}

	slog.Info("tts.preview: received", "component", "mqtt", "provider", req.Provider, "voice", req.Voice, "language", req.Language)

	// Ack immediately so BFF knows the device received the command.
	if err := h.publishDataResult(domain.KindTTSPreview, "starting", "", nil); err != nil {
		slog.Warn("tts.preview: publish starting ack failed", "component", "mqtt", "error", err)
	}

	go func() {
		apiKey := h.config.GetTTSAPIKey()
		baseURL := h.config.GetTTSBaseURL()
		if err := hal.SpeakPreview(req.Text, req.Voice, req.Provider, apiKey, baseURL, req.Speed); err != nil {
			slog.Error("tts.preview: SpeakPreview failed", "component", "mqtt", "error", err)
			if pubErr := h.publishDataResult(domain.KindTTSPreview, "failure", err.Error(), nil); pubErr != nil {
				slog.Warn("tts.preview: publish failure ack failed", "component", "mqtt", "error", pubErr)
			}
			return
		}
		slog.Info("tts.preview: played", "component", "mqtt", "provider", req.Provider, "voice", req.Voice)
		if err := h.publishDataResult(domain.KindTTSPreview, "success", "", nil); err != nil {
			slog.Warn("tts.preview: publish success ack failed", "component", "mqtt", "error", err)
		}
	}()

	return nil
}
