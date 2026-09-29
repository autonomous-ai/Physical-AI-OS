package mqtthandler

import (
	"log/slog"
	"os"
	"os/exec"

	"go.autonomous.ai/os/system/domain"
)

// configPath is the config.json path config.Load() reads (relative to
// WorkingDirectory=/root).
const configPath = "config/config.json"

// handleDeviceSoftReset wipes the on-disk config and restarts os-server so
// the device drops back into AP setup mode WITHOUT rebooting or rolling back
// the firmware.
func (h *DeviceMQTTHandler) handleDeviceSoftReset(_ domain.MQTTDataCommand) error {
	slog.Info("device.soft_reset: received — wiping config + restarting", "component", "mqtt")

	go func() {
		h.alertOps("♻️ device.soft_reset — wiping config + restarting", "")
		if err := os.Remove(configPath); err != nil && !os.IsNotExist(err) {
			slog.Error("device.soft_reset: remove config failed", "component", "mqtt", "path", configPath, "error", err)
		} else {
			slog.Info("device.soft_reset: config wiped", "component", "mqtt", "path", configPath)
		}
		slog.Info("device.soft_reset: restarting os-server", "component", "mqtt")
		if err := exec.Command("systemctl", "restart", "os-server").Run(); err != nil {
			slog.Error("device.soft_reset: systemctl restart failed, falling back to os.Exit", "component", "mqtt", "error", err)
			os.Exit(0)
		}
	}()

	return nil
}
