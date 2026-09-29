package codex

import (
	_ "embed"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/runtimereg"
)

// InstallScript is the device-side installer for the Codex backend, embedded in
// os-server so it ships + OTA-updates with the binary.
//
//go:embed install.sh
var InstallScript []byte

// PresyncScript is the device-side pre-start hook for Codex.
//
//go:embed presync.sh
var PresyncScript []byte

// ReadyScript verifies the authenticated Codex bridge WebSocket upgrade.
//
//go:embed ready.sh
var ReadyScript []byte

// Register the embedded installer + presync so system/device can materialize
// them without importing this package (which would cycle via statusled → device).
func init() {
	runtimereg.Register(domain.AgentRuntimeCodex, InstallScript)
	runtimereg.RegisterPresync(domain.AgentRuntimeCodex, PresyncScript)
	runtimereg.RegisterReadiness(domain.AgentRuntimeCodex, ReadyScript)
}
