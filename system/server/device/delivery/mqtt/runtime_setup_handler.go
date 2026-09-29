package mqtthandler

import (
	"log/slog"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
)

func (h *DeviceMQTTHandler) publishRuntimeSetupAck(kind, status, errMsg string, data *domain.AgentRuntimeSetData) {
	ack := domain.AgentRuntimeSetAck{
		MQTTInfoResponse: domain.NewMQTTInfoResponse(h.config, "data", device.GetDeviceMac()),
		Kind:             kind,
		Status:           status,
		Error:            errMsg,
		Data:             data,
	}
	if err := h.publish(ack); err != nil {
		slog.Warn("runtime setup: publish ack failed", "component", "mqtt", "kind", kind, "status", status, "error", err)
	}
}

// handleRuntimeSetup is shared by the hermes.setup and picoclaw.setup dispatch
// cases; runtime is the target backend named by the kind.
func (h *DeviceMQTTHandler) handleRuntimeSetup(env domain.MQTTDataCommand, runtime string) error {
	kind := env.Kind
	req := domain.AgentRuntimeSetData{Runtime: runtime}

	slog.Info("runtime setup: received", "component", "mqtt", "kind", kind, "runtime", runtime)

	// A systemd-active target that has not yet bound its gateway or accepted
	// its protocol must fail and roll back rather than receive a success ack.
	run, err := h.deviceService.ReserveAgentRuntimeSwitchReady(req)
	if err != nil {
		slog.Warn("runtime setup: switch already in progress", "component", "mqtt", "kind", kind, "runtime", runtime)
		h.publishRuntimeSetupAck(kind, "failure", err.Error(), &req)
		h.alertOps("🔴 Runtime setup "+kind+" — FAILED", err.Error())
		return nil
	}

	h.publishRuntimeSetupAck(kind, "starting", "", nil)
	h.alertOps("🚀 Runtime setup "+kind+" — starting", "")

	go func() {
		switched, err := run()
		if err != nil {
			slog.Error("runtime setup: switch failed", "component", "mqtt", "kind", kind, "error", err)
			h.publishRuntimeSetupAck(kind, "failure", err.Error(), &req)
			h.alertOps("🔴 Runtime setup "+kind+" — FAILED", err.Error())
			return
		}
		// Switch confirmed readiness (or was a no-op). Ack success — it must reach
		// the wire BEFORE the os-server restart below, which kills us.
		slog.Info("runtime setup: switch confirmed", "component", "mqtt", "kind", kind, "runtime", runtime, "switched", switched)
		h.publishRuntimeSetupAck(kind, "success", "", &req)
		h.alertOps("🟢 Runtime setup "+kind+" — SUCCESS", "")

		if switched {
			// Restart only after the success ack: the restart kills this process.
			if rerr := h.deviceService.RestartForAgentRuntime(); rerr != nil {
				slog.Error("runtime setup: os-server restart failed", "component", "mqtt", "kind", kind, "error", rerr)
			}
		}
	}()

	return nil
}
