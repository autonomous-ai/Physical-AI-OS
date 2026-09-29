package mqtthandler

import (
	"encoding/json"
	"log/slog"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
)

func (h *DeviceMQTTHandler) publishTTSSetAck(status, errMsg string, data *domain.MQTTTTSSetData) {
	ack := domain.MQTTTTSSetAck{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "data", device.GetDeviceMac()),
		Kind:             domain.KindTTSSet,
		Status:           status,
		Error:            errMsg,
		Data:             data,
	}
	if err := h.publish(ack); err != nil {
		slog.Warn("tts.set: publish ack failed", "component", "mqtt", "status", status, "error", err)
	}
}

func (h *DeviceMQTTHandler) handleTTSSet(env domain.MQTTDataCommand) error {
	var req domain.MQTTTTSSetData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		slog.Error("tts.set: invalid payload", "component", "mqtt", "error", err)
		h.publishTTSSetAck("failure", "invalid JSON payload", nil)
		return err
	}

	if err := domain.ValidateTTSSpeed(req.Speed); err != nil {
		h.publishTTSSetAck("failure", err.Error(), &req)
		return err
	}
	slog.Info("tts.set: received", "component", "mqtt", "provider", req.Provider, "voice", req.Voice, "language", req.Language)

	h.publishTTSSetAck("starting", "", nil)

	go func() {
		if err := h.deviceService.UpdateVoiceConfig(req.Provider, req.Voice, req.Language, req.Speed); err != nil {
			slog.Error("tts.set: UpdateVoiceConfig failed", "component", "mqtt", "error", err)
			h.publishTTSSetAck("failure", err.Error(), &req)
			return
		}
		slog.Info("tts.set: applied", "component", "mqtt", "provider", req.Provider, "voice", req.Voice, "language", req.Language)
		h.publishTTSSetAck("success", "", &req)
	}()

	return nil
}
