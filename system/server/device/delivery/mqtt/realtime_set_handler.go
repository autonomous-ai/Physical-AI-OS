package mqtthandler

import (
	"encoding/json"
	"log/slog"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

func (h *DeviceMQTTHandler) publishRealtimeSetAck(status, errMsg string, data *domain.RealtimeSetData) {
	if data != nil && data.APIKey != "" {
		// Never echo the key back over fd_channel.
		redacted := *data
		redacted.APIKey = ""
		data = &redacted
	}
	ack := domain.MQTTRealtimeSetAck{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "data", device.GetDeviceMac()),
		Kind:             domain.KindRealtimeSet,
		Status:           status,
		Error:            errMsg,
		Data:             data,
	}
	if err := h.publish(ack); err != nil {
		slog.Warn("realtime.set: publish ack failed", "component", "mqtt", "status", status, "error", err)
	}
}

func (h *DeviceMQTTHandler) handleRealtimeSet(env domain.MQTTDataCommand) error {
	var req domain.RealtimeSetData
	if err := json.Unmarshal(env.Data, &req); err != nil {
		slog.Error("realtime.set: invalid payload", "component", "mqtt", "error", err)
		h.publishRealtimeSetAck("failure", "invalid JSON payload", nil)
		return err
	}

	slog.Info("realtime.set: received", "component", "mqtt", "provider", req.Provider, "voice", req.Voice, "reasoning", req.Reasoning)

	h.publishRealtimeSetAck("starting", "", nil)

	go func() {
		if err := h.deviceService.UpdateRealtimeConfig(req); err != nil {
			slog.Error("realtime.set: UpdateRealtimeConfig failed", "component", "mqtt", "error", err)
			h.publishRealtimeSetAck("failure", err.Error(), &req)
			return
		}
		slog.Info("realtime.set: applied", "component", "mqtt", "provider", req.Provider, "voice", req.Voice)
		h.publishRealtimeSetAck("success", "", &req)
	}()

	return nil
}

// handleRealtimeGet handles kind="realtime.get": the saved realtime settings
// (the key only as has_api_key) and the provider/voice/reasoning lists the
// app should offer, the same data the web Realtime page loads. Synchronous.
func (h *DeviceMQTTHandler) handleRealtimeGet(env domain.MQTTDataCommand) error {
	return h.publishDataResult(domain.KindRealtimeGet, "success", "", domain.MQTTRealtimeGetData{
		Config:  h.deviceService.RealtimePublic(),
		Options: config.GetRealtimeOptions(),
	})
}
