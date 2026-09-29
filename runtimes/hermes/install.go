package hermes

import (
	_ "embed"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/runtimereg"
)

// InstallScript is the embedded device-side Hermes installer run by switch-runtime on first switch.
//
//go:embed install.sh
var InstallScript []byte

// PresyncScript is the embedded pre-start hook switch-runtime runs before hermes starts.
//
//go:embed presync.sh
var PresyncScript []byte

// ReadinessScript verifies that the Hermes HTTP gateway accepts authenticated requests after systemd has started its process.
//
//go:embed ready.sh
var ReadinessScript []byte

// Register the embedded installer + presync so system/device can materialize them without importing this package (which would cycle via statusled → device).
func init() {
	runtimereg.Register(domain.AgentRuntimeHermes, InstallScript)
	runtimereg.RegisterPresync(domain.AgentRuntimeHermes, PresyncScript)
	runtimereg.RegisterReadiness(domain.AgentRuntimeHermes, ReadinessScript)
}
