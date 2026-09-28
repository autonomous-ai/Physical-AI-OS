package mqtthandler

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"sync/atomic"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/harness"
)

// SetHarnessVoiceController shares the HTTP/HAL controller with MQTT. Production
// allocates the atomic holder before subscribing; tests may attach before use.
func (h *DeviceMQTTHandler) SetHarnessVoiceController(v *harness.VoiceController) {
	if h.harnessVoice == nil {
		h.harnessVoice = &atomic.Pointer[harness.VoiceController]{}
	}
	h.harnessVoice.Store(v)
}

func (h *DeviceMQTTHandler) harnessVoiceController() *harness.VoiceController {
	if h.harnessVoice == nil {
		return nil
	}
	return h.harnessVoice.Load()
}

func (h *DeviceMQTTHandler) handleHarnessVoiceMode(env domain.MQTTDataCommand) error {
	voice := h.harnessVoiceController()
	if voice == nil {
		return h.publishDataResult(env.Kind, "failure", "Harness voice service unavailable", nil)
	}
	if env.Kind == domain.KindHarnessVoiceModeGet {
		return h.publishDataResult(env.Kind, "success", "", voice.SupportState(context.Background()))
	}
	var req struct {
		Enabled *bool `json:"enabled"`
	}
	decoder := json.NewDecoder(bytes.NewReader(env.Data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&req); err != nil || req.Enabled == nil {
		return h.publishDataResult(env.Kind, "failure", "Expected data containing only enabled: true or false", nil)
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return h.publishDataResult(env.Kind, "failure", "Invalid Harness voice request", nil)
	}
	// Explicit assignment makes duplicate MQTT deliveries harmless; never invert.
	state, err := voice.SetMode(context.Background(), *req.Enabled)
	if err != nil {
		return h.publishDataResult(env.Kind, "failure", err.Error(), nil)
	}
	return h.publishDataResult(env.Kind, "success", "", state)
}
