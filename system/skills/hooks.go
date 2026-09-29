package skills

import "go.autonomous.ai/os/system/device"

// Hooks is the full set of hooks the OS publishes.
var Hooks = []string{
	"emotion-acknowledge",
	"turn-gate",
}

// HookCapability maps a hook to its required ROBOT.md capability; absent means always installed.
var HookCapability = map[string]string{
	"emotion-acknowledge": device.CapExpression,
}

// SupportedHooks filters Hooks by deviceCaps like Supported; fail-open on empty caps.
func SupportedHooks(deviceCaps map[string]bool) []string {
	if len(deviceCaps) == 0 {
		return Hooks
	}
	out := make([]string, 0, len(Hooks))
	for _, name := range Hooks {
		if cap := HookCapability[name]; cap == "" || deviceCaps[cap] {
			out = append(out, name)
		}
	}
	return out
}
