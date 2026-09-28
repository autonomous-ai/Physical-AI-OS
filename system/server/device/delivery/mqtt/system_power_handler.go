package mqtthandler

import (
	"log/slog"

	"go.autonomous.ai/os/system/domain"
	systemshell "go.autonomous.ai/os/system/server/system"
)

// handleSystemPower publishes its acknowledgement before scheduling the power
// action.
func (h *DeviceMQTTHandler) handleSystemPower(env domain.MQTTDataCommand, action string) error {
	if err := h.publishDataResult(env.Kind, "starting", "", map[string]any{
		"started": true,
		"action":  action,
	}); err != nil {
		return err
	}

	var started bool
	var reason string
	if action == "reboot" {
		started, reason = systemshell.TriggerReboot()
	} else {
		started, reason = systemshell.TriggerShutdown()
	}
	if !started {
		slog.Warn("system power action rejected", "component", "mqtt", "action", action, "reason", reason)
		return h.publishDataResult(env.Kind, "failure", reason, nil)
	}
	slog.Info("system power action queued", "component", "mqtt", "action", action)
	return nil
}

func (h *DeviceMQTTHandler) handleSystemReboot(env domain.MQTTDataCommand) error {
	return h.handleSystemPower(env, "reboot")
}

func (h *DeviceMQTTHandler) handleSystemShutdown(env domain.MQTTDataCommand) error {
	return h.handleSystemPower(env, "shutdown")
}
