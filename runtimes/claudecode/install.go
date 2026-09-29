package claudecode

import (
	_ "embed"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/runtimereg"
)

// InstallScript is the device-side installer for the Claude Code backend, embedded in
// os-server so it ships + OTA-updates with the binary (no CDN round-trip needed).
//
//go:embed install.sh
var InstallScript []byte

// PresyncScript is the device-side pre-start hook for Claude Code.
//
//go:embed presync.sh
var PresyncScript []byte

// ReadyScript verifies the authenticated Claude Code bridge WebSocket upgrade.
//
//go:embed ready.sh
var ReadyScript []byte

// Register the embedded installer + presync so system/device can materialize
// them without importing this package (which would cycle via statusled → device).
func init() {
	runtimereg.Register(domain.AgentRuntimeClaudeCode, InstallScript)
	runtimereg.RegisterPresync(domain.AgentRuntimeClaudeCode, PresyncScript)
	runtimereg.RegisterReadiness(domain.AgentRuntimeClaudeCode, ReadyScript)
}
