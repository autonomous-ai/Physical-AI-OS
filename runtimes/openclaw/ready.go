package openclaw

import (
	_ "embed"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/runtimereg"
)

// ReadyScript performs OpenClaw's own authenticated RPC readiness probe rather than treating its systemd process or open TCP port as usable.
//
//go:embed ready.sh
var ReadyScript []byte

func init() {
	runtimereg.RegisterReadiness(domain.AgentRuntimeOpenClaw, ReadyScript)
}
