package mqtthandler

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
)

// channelRefreshTimeout caps the whole channel.refresh_config call.
const channelRefreshTimeout = 5 * time.Minute

// handleChannelRefreshConfig handles kind="channel.refresh_config":
// re-applies the canonical channels.<channel> block on an already-onboarded
// device using the current applySlackChannelConfig writer.
func (h *DeviceMQTTHandler) handleChannelRefreshConfig(env domain.MQTTDataCommand) error {
	var req domain.MQTTChannelRefreshConfigData
	if len(env.Data) > 0 {
		if err := json.Unmarshal(env.Data, &req); err != nil {
			slog.Error("channel.refresh_config: invalid payload", "component", "mqtt", "kind", env.Kind, "error", err)
			return h.publishDataResult(env.Kind, "failure", "invalid channel.refresh_config data: "+err.Error(), nil)
		}
	}
	if req.Channel == "" {
		return h.publishDataResult(env.Kind, "failure", "channel is required", nil)
	}

	slog.Info("channel.refresh_config: received", "component", "mqtt", "channel", req.Channel)

	if err := h.publishDataResult(env.Kind, "configuring", "", nil); err != nil {
		slog.Warn("channel.refresh_config: ack publish failed", "component", "mqtt", "channel", req.Channel, "error", err)
	}

	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), channelRefreshTimeout)
		defer cancel()

		runtimeStr, err := h.deviceService.RefreshChannelConfig(ctx, req.Channel)
		if err != nil {
			errCode := err.Error()
			switch {
			case errors.Is(err, device.ErrSlackCredentialsMissing):
				errCode = "slack_credentials_missing"
			case errors.Is(err, device.ErrChannelNotSupported):
				errCode = "channel_not_supported"
			}
			slog.Error("channel.refresh_config: failed", "component", "mqtt", "channel", req.Channel, "code", errCode, "runtime", runtimeStr, "error", err)
			h.alertOps("❌ refresh_channel "+req.Channel+" — FAILED", errCode)
			_ = h.publishDataResult(env.Kind, "failure", errCode, domain.MQTTChannelRefreshConfigResultData{
				Channel: req.Channel,
				Runtime: runtimeStr,
			})
			return
		}
		slog.Info("channel.refresh_config: success", "component", "mqtt", "channel", req.Channel, "runtime", runtimeStr)
		h.alertOps("✅ refresh_channel "+req.Channel+" — OK", "runtime="+runtimeStr)
		_ = h.publishDataResult(env.Kind, "success", "", domain.MQTTChannelRefreshConfigResultData{
			Channel: req.Channel,
			Runtime: runtimeStr,
		})
	}()
	return nil
}
