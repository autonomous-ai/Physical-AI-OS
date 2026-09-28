package mqtthandler

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"time"

	"github.com/go-playground/validator/v10"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
)

// pairingQRFormat is the wire-format identifier for the QR text we ship.
// Each char is two vertical QR pixels: '█'=both '▀'=top '▄'=bottom ' '=neither.
const pairingQRFormat = "unicode_blocks_2x1"

// addChannelTimeout caps the whole add_channel call (incl. streaming pairing).
// Budget: ~90s plugin install + 90s QR-scan window + 5min Baileys post-pair
// sync + slack. Set above the sum of those caps so a slow but successful
// flow can't be cut short.
const addChannelTimeout = 10 * time.Minute

func (h *DeviceMQTTHandler) publishAddChannelResult(channel, status, errMsg string, evt *domain.PairingEvent) error {
	resp := domain.MQTTAddChannelResponse{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "add_channel", device.GetDeviceMac()),
		Channel:          channel,
		Status:           status,
		Error:            errMsg,
	}
	if evt != nil && evt.Status == domain.PairingStatusQR {
		resp.PairingQRText = evt.QRText
		resp.PairingQRFormat = pairingQRFormat
		resp.PairingQRSeq = evt.QRSeq
		if !evt.ExpiresAt.IsZero() {
			resp.PairingExpiresAt = evt.ExpiresAt.UTC().Format(time.RFC3339)
		}
	}
	return h.publish(resp)
}

// handleAddChannel handles the legacy top-level cmd:"add_channel" message
// (config carried inline on MQTT). Kept for backward compatibility; the
// preferred path is the data-envelope form (see handleAddChannelData) which
// supports the privacy-typed fetch flow so credentials never travel inline
// over MQTT.
func (h *DeviceMQTTHandler) handleAddChannel(cmd domain.MQTTMessage) error {
	var req domain.MQTTAddChannelCommand
	if err := json.Unmarshal(cmd.Raw(), &req); err != nil {
		slog.Error("add_channel: invalid payload", "component", "mqtt", "error", err)
		return h.publishAddChannelResult(req.Channel, "failure", "invalid JSON payload", nil)
	}
	return h.processAddChannel(req)
}

// handleAddChannelData handles the cmd:"data" kind:"add_channel" form, whose
// {channel, config} payload arrives in env.Data — inline, or fetched over TLS
// when the backend sent it as a type:"privacy" envelope (see
// privacy_fetch.go). Routing through dispatchData means privacy support is
// automatic; the core logic is shared with the legacy handler via
// processAddChannel.
func (h *DeviceMQTTHandler) handleAddChannelData(env domain.MQTTDataCommand) error {
	// Metadata only — env.Data carries channel credentials (bot tokens,
	// BlueBubbles password) so raw bytes must never hit journalctl. The
	// parsed shape below is safer: channel name + config_keys is enough
	// to diagnose a key-name mismatch without exposing the values.
	slog.Info("add_channel data envelope received",
		"component", "mqtt",
		"kind", env.Kind,
		"type", env.Type,
		"channel_hint", env.Channel,
		"data_len", len(env.Data),
	)
	var req domain.MQTTAddChannelCommand
	if len(env.Data) > 0 {
		if err := json.Unmarshal(env.Data, &req); err != nil {
			slog.Error("add_channel (data): invalid payload", "component", "mqtt", "error", err)
			return h.publishAddChannelResult(req.Channel, "failure", "invalid JSON payload", nil)
		}
		// Tolerate the alternate shape where the channel config rides under
		// "data" instead of "config". If "config" is absent/empty, fall back
		// to "data" so either {channel, config:{…}} or {channel, data:{…}}
		// is accepted — matches the intern v1 behaviour ported from
		// openclaw-lobster's handleAddChannelData.
		if len(req.Config) == 0 {
			var alt struct {
				Data map[string]interface{} `json:"data"`
			}
			if err := json.Unmarshal(env.Data, &alt); err == nil && len(alt.Data) > 0 {
				req.Config = alt.Data
			}
		}
	}
	// Keys only, never values. Enough to spot a rename ("server_url" vs
	// "bluebubbles_server_url") without leaking the credential itself.
	cfgKeys := make([]string, 0, len(req.Config))
	for k := range req.Config {
		cfgKeys = append(cfgKeys, k)
	}
	slog.Info("add_channel data parsed",
		"component", "mqtt",
		"channel", req.Channel,
		"config_keys", cfgKeys,
	)
	return h.processAddChannel(req)
}

// processAddChannel is the shared add-channel logic for both the legacy
// cmd:"add_channel" form and the cmd:"data" kind:"add_channel" form
// (inline or privacy-fetched). Kept as a single implementation so a bug fix
// or new channel case only needs to land once.
func (h *DeviceMQTTHandler) processAddChannel(req domain.MQTTAddChannelCommand) error {
	channelReq := req.ToRequest()
	if err := validator.New().Struct(channelReq); err != nil {
		return h.publishAddChannelResult(req.Channel, "failure", err.Error(), nil)
	}
	if err := channelReq.ValidateChannel(); err != nil {
		return h.publishAddChannelResult(req.Channel, "failure", err.Error(), nil)
	}

	ctx, cancel := context.WithTimeout(context.Background(), addChannelTimeout)
	defer cancel()

	events, err := h.deviceService.AddChannel(ctx, channelReq)
	if err != nil {
		// Map the shared sentinel to a stable code so the backend can branch
		// without parsing free-form text (mirrors channel.refresh_config).
		errMsg := err.Error()
		if errors.Is(err, device.ErrChannelNotSupported) {
			errMsg = "channel_not_supported"
		}
		slog.Error("add_channel: failed", "component", "mqtt", "channel", req.Channel, "code", errMsg, "error", err)
		h.alertOps("❌ add_channel "+req.Channel+" — FAILED", errMsg)
		return h.publishAddChannelResult(req.Channel, "failure", errMsg, nil)
	}

	if events == nil {
		slog.Info("add_channel: success", "component", "mqtt", "channel", req.Channel)
		h.alertOps("✅ add_channel "+req.Channel+" — OK", "")
		return h.publishAddChannelResult(req.Channel, "success", "", nil)
	}

	// WhatsApp streams pairing events. Publish one fd_channel message per event.
	for evt := range events {
		status := string(evt.Status)
		if pubErr := h.publishAddChannelResult(req.Channel, status, evt.Error, &evt); pubErr != nil {
			slog.Error("add_channel: publish event failed", "component", "mqtt", "status", status, "error", pubErr)
			// Keep draining so the goroutine in PairWhatsapp can exit cleanly.
		}
		h.alertPairingTerminal("add_channel "+req.Channel, evt)
	}
	slog.Info("add_channel: pairing stream closed", "component", "mqtt", "channel", req.Channel)
	return nil
}
