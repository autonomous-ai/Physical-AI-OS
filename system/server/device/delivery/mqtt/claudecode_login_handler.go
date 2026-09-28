package mqtthandler

import (
	"context"
	"encoding/json"
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
)

// claudeLoginTimeout caps the whole OAuth login flow (the user pastes a code
// back from another device).
const claudeLoginTimeout = 10 * time.Minute

func (h *DeviceMQTTHandler) publishClaudeLoginResult(status, errMsg string, evt *domain.PairingEvent) error {
	resp := domain.MQTTClaudeCodeLoginResponse{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, domain.CommandClaudeCodeLogin, device.GetDeviceMac()),
		Status:           status,
		Error:            errMsg,
	}
	if evt != nil && evt.Status == domain.PairingStatusURL {
		resp.LoginURL = evt.URL
	}
	return h.publish(resp)
}

// handleClaudeCodeLogin starts the claude.ai OAuth login flow and streams its
// pairing events to fd_channel.
func (h *DeviceMQTTHandler) handleClaudeCodeLogin(_ domain.MQTTMessage) error {
	ctx, cancel := context.WithTimeout(context.Background(), claudeLoginTimeout)

	events := h.deviceService.StartClaudeLogin(ctx)
	go func() {
		defer cancel()
		for evt := range events {
			status := string(evt.Status)
			if err := h.publishClaudeLoginResult(status, evt.Error, &evt); err != nil {
				slog.Error("claudecode_login: publish event failed", "component", "mqtt", "status", status, "error", err)
			}
			h.alertPairingTerminal("claude_code login", evt)
		}
		slog.Info("claudecode_login: stream closed", "component", "mqtt")
	}()
	return nil
}

// handleClaudeCodeLoginCode feeds the pasted authorization code into the
// waiting login flow and acks the submission.
func (h *DeviceMQTTHandler) handleClaudeCodeLoginCode(cmd domain.MQTTMessage) error {
	var req domain.MQTTClaudeCodeLoginCodeCommand
	if err := json.Unmarshal(cmd.Raw(), &req); err != nil {
		return h.publishClaudeLoginResult("failure", "invalid claudecode_login_code payload: "+err.Error(), nil)
	}
	if err := h.deviceService.SubmitClaudeLoginCode(req.Code); err != nil {
		return h.publishClaudeLoginResult("failure", err.Error(), nil)
	}
	return h.publishClaudeLoginResult("code_accepted", "", nil)
}
