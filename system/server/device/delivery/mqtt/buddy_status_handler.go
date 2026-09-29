package mqtthandler

import (
	"context"
	"encoding/json"
	"log/slog"

	"go.autonomous.ai/os/system/buddy"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
)

func (h *DeviceMQTTHandler) handleBuddyStatus(env domain.MQTTDataCommand) error {
	if h.buddyService == nil {
		return h.publishDataResult(env.Kind, "failure", "buddy service unavailable", nil)
	}
	return h.publishBuddyStatus(context.Background())
}

// StartBuddyStatusLoop publishes current state at startup and after changes.
func (h *DeviceMQTTHandler) StartBuddyStatusLoop(ctx context.Context) {
	if h.buddyService == nil {
		return
	}
	for {
		if ctx.Err() != nil {
			return
		}
		if h.config.DeviceID != "" && h.config.FDChannel != "" {
			if err := h.publishBuddyStatus(ctx); err != nil && ctx.Err() == nil {
				slog.Warn("buddy status publish failed", "component", "mqtt", "error", err)
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-h.buddyService.StatusChanges():
		}
	}
}

func (h *DeviceMQTTHandler) StartHarnessStatusLoop(ctx context.Context) {
	if h.harnessService == nil {
		return
	}
	for {
		select {
		case <-ctx.Done():
			return
		case <-h.harnessService.StatusChanges():
			_ = h.publishHarnessStatus(ctx)
		}
	}
}
func (h *DeviceMQTTHandler) publishHarnessStatus(parent context.Context) error {
	ctx, cancel := context.WithTimeout(parent, publishTimeout)
	defer cancel()
	client := h.mqttFactory.GetClient("harness-status-" + buddy.NewCommandID())
	if err := client.Connect(ctx); err != nil {
		return err
	}
	defer client.Close()
	response := domain.MQTTDataResponse{MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "data", device.GetDeviceMac()), Kind: domain.KindHarnessStatus, Status: "success", Data: h.harnessService.Status()}
	payload, err := json.Marshal(response)
	if err != nil {
		return err
	}
	return client.Publish(ctx, h.config.FDChannel, 1, payload)
}

func (h *DeviceMQTTHandler) publishBuddyStatus(parent context.Context) error {
	ctx, cancel := context.WithTimeout(parent, publishTimeout)
	defer cancel()
	client := h.mqttFactory.GetClient("buddy-status-" + buddy.NewCommandID())
	if err := client.Connect(ctx); err != nil {
		return err
	}
	defer client.Close()
	response := domain.MQTTDataResponse{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "data", device.GetDeviceMac()),
		Kind:             domain.KindBuddyStatus,
		Status:           "success",
		Data:             h.buddyService.Status(),
	}
	payload, err := json.Marshal(response)
	if err != nil {
		return err
	}
	return client.Publish(ctx, h.config.FDChannel, 1, payload)
}
