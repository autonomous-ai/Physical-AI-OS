package server

import (
	"go.autonomous.ai/os/system/device"
	_agentHttp "go.autonomous.ai/os/system/server/agent/delivery/http"
	"go.autonomous.ai/os/system/server/config"
)

// provideStatusLEDHasLight resolves the `light` capability for the running
// device so statusled can no-op cleanly on devices without an LED.
func provideStatusLEDHasLight(cfg *config.Config) bool {
	return device.Has(cfg.DeviceTypeOrDefault(), device.CapLight)
}

// provideAgentIsSleeping exposes the agent handler's sleep state as the
// func() bool dependency SensingHandler declares.
func provideAgentIsSleeping(h *_agentHttp.AgentHandler) func() bool {
	return h.IsSleeping
}
