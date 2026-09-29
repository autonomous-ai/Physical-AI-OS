package mqtthandler

import (
	"encoding/json"
	"errors"
	"log/slog"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
)

var errWakeWordEnabledRequired = errors.New("enabled is required")

func (h *DeviceMQTTHandler) publishWakeWordGateAck(status, errMsg string, data *domain.WakeWordGateData) {
	ack := domain.MQTTWakeWordGateAck{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "data", device.GetDeviceMac()),
		Kind:             domain.KindWakeWordGate,
		Status:           status,
		Error:            errMsg,
		Data:             data,
	}
	if err := h.publish(ack); err != nil {
		slog.Warn("wakeword.gate: publish ack failed", "component", "mqtt", "status", status, "error", err)
	}
}

func (h *DeviceMQTTHandler) handleWakeWordGate(env domain.MQTTDataCommand) error {
	var req domain.WakeWordGateData
	if err := json.Unmarshal(env.Data, &req); err != nil || req.Enabled == nil {
		if err == nil {
			err = errWakeWordEnabledRequired
		}
		slog.Error("wakeword.gate: invalid payload", "component", "mqtt", "error", err)
		h.publishWakeWordGateAck("failure", "enabled is required", nil)
		return err
	}

	slog.Info("wakeword.gate: received", "component", "mqtt", "enabled", *req.Enabled)
	h.publishWakeWordGateAck("starting", "", nil)

	go func() {
		if err := h.deviceService.UpdateWakeWord(*req.Enabled); err != nil {
			slog.Error("wakeword.gate: UpdateWakeWord failed", "component", "mqtt", "error", err)
			h.publishWakeWordGateAck("failure", err.Error(), &req)
			return
		}
		slog.Info("wakeword.gate: applied", "component", "mqtt", "enabled", *req.Enabled)
		h.publishWakeWordGateAck("success", "", &req)
	}()

	return nil
}
