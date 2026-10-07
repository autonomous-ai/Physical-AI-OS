package mqtthandler

import (
	"encoding/json"
	"log/slog"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

func (h *DeviceMQTTHandler) publishVoiceInputModeAck(status, errMsg string, data *domain.VoiceInputModeData) {
	ack := domain.MQTTVoiceInputModeAck{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "data", device.GetDeviceMac()),
		Kind:             domain.KindVoiceInputMode, Status: status, Error: errMsg, Data: data,
	}
	if err := h.publish(ack); err != nil {
		slog.Warn("voice.input_mode: publish ack failed", "component", "mqtt", "status", status, "error", err)
	}
}

func (h *DeviceMQTTHandler) handleVoiceInputMode(env domain.MQTTDataCommand) error {
	var req domain.VoiceInputModeData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		h.publishVoiceInputModeAck("failure", err.Error(), nil)
		return err
	}
	if err := config.ValidateVoiceInputMode(req.Mode); err != nil {
		h.publishVoiceInputModeAck("failure", err.Error(), nil)
		return err
	}
	h.publishVoiceInputModeAck("starting", "", nil)
	go func() {
		if err := h.deviceService.UpdateVoiceInputMode(req.Mode); err != nil {
			h.publishVoiceInputModeAck("failure", err.Error(), &req)
			return
		}
		h.publishVoiceInputModeAck("success", "", &req)
	}()
	return nil
}
