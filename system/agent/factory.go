package agent

import (
	"log/slog"

	"go.autonomous.ai/os/runtimes/claudecode"
	"go.autonomous.ai/os/runtimes/codex"
	"go.autonomous.ai/os/runtimes/hermes"
	"go.autonomous.ai/os/runtimes/openclaw"
	"go.autonomous.ai/os/runtimes/opencode"
	"go.autonomous.ai/os/runtimes/picoclaw"
	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/monitor"
	"go.autonomous.ai/os/system/server/config"
	"go.autonomous.ai/os/system/statusled"
)

// gatewayTransport is the wire transport each runtime speaks; ROBOT.md
// gateway.protocol is only validated against it.
var gatewayTransport = map[string]string{
	"openclaw":   "websocket",
	"hermes":     "sse",
	"picoclaw":   "websocket",
	"codex":      "websocket",
	"claudecode": "websocket",
	"opencode":   "websocket",
	// "remote" is Hermes-over-LAN, same transport as "hermes".
	"remote": "sse",
}

// ProvideGateway returns the AgentGateway for the resolved runtime (see resolveRuntime).
func ProvideGateway(cfg *config.Config, bus *monitor.Bus, sled *statusled.Service) domain.AgentGateway {
	// Warn only: gateway.protocol never drives transport selection.
	devType := cfg.DeviceTypeOrDefault()
	if proto := device.GatewayProtocol(devType); proto != "" {
		if def := device.GatewayDefault(devType); def != "" {
			if want, ok := gatewayTransport[def]; ok && want != proto {
				slog.Warn("ROBOT.md gateway.protocol contradicts gateway.default's transport",
					"component", "agent", "device_type", devType,
					"gateway.default", def, "gateway.protocol", proto, "expected", want)
			}
		}
	}

	eff, raw_runtime, source := resolveRuntime(cfg)
	switch eff {
	case "hermes":
		logBackendBanner("HERMES", map[string]string{
			"base_url":     hermes.BaseURL,
			"conversation": hermes.Conversation,
			"model":        hermes.Model,
			"api_key_set":  boolStr(hermes.APIKey != ""),
			"source":       source,
		})
		return hermes.ProvideService(cfg, bus, sled)
	case "remote":
		// Endpoint override must precede ProvideService; the client reads the
		// package vars at request time.
		hermes.ApplyExternalEndpoint(cfg.AgentRemoteURL, cfg.AgentRemoteToken)
		logBackendBanner("HERMES-REMOTE", map[string]string{
			"base_url":     hermes.BaseURL,
			"conversation": hermes.Conversation,
			"model":        hermes.Model,
			"api_key_set":  boolStr(hermes.APIKey != ""),
			"source":       source,
		})
		return hermes.ProvideService(cfg, bus, sled)
	case "picoclaw":
		logBackendBanner("PICOCLAW", map[string]string{
			"ws_url":       picoclaw.WSURL,
			"conversation": picoclaw.Conversation,
			"source":       source,
		})
		return picoclaw.ProvideService(cfg, bus, sled)
	case "codex":
		logBackendBanner("CODEX", map[string]string{
			"ws_url":       codex.WSURL,
			"conversation": codex.Conversation,
			"source":       source,
		})
		return codex.ProvideService(cfg, bus, sled)
	case "claudecode":
		logBackendBanner("CLAUDECODE", map[string]string{
			"ws_url": claudecode.WSURL,
			"source": source,
		})
		return claudecode.ProvideService(cfg, bus, sled)
	case "opencode":
		logBackendBanner("OPENCODE", map[string]string{
			"ws_url":       opencode.WSURL,
			"conversation": opencode.Conversation,
			"source":       source,
		})
		return opencode.ProvideService(cfg, bus, sled)
	default:
		effective := raw_runtime
		if effective == "" {
			effective = "openclaw (default — agent_runtime + gateway.default both unset)"
		} else if effective != "openclaw" {
			effective = "openclaw (FALLBACK — unknown runtime=" + raw_runtime + ")"
		}
		logBackendBanner("OPENCLAW", map[string]string{
			"config_dir":      cfg.OpenclawConfigDir,
			"effective_value": effective,
			"source":          source,
		})
		return openclaw.ProvideService(cfg, bus, sled)
	}
}

// resolveRuntime returns the effective runtime, the raw value and its source.
// Order: config.agent_runtime > device.ResolveDefaultAgent > "openclaw". It must share
// ResolveDefaultAgent with the boot seed so it never disagrees with the persisted value.
func resolveRuntime(cfg *config.Config) (effective, raw, source string) {
	raw = cfg.AgentRuntime
	source = "config.agent_runtime"
	if raw == "" {
		if g, src := device.ResolveDefaultAgent(cfg); g != "" {
			raw, source = g, src
		}
	}
	switch raw {
	case "hermes":
		return "hermes", raw, source
	case "picoclaw":
		return "picoclaw", raw, source
	case "codex":
		return "codex", raw, source
	case "claudecode":
		return "claudecode", raw, source
	case "opencode":
		return "opencode", raw, source
	case "remote":
		return "remote", raw, source
	default:
		return "openclaw", raw, source
	}
}

func logBackendBanner(name string, fields map[string]string) {
	args := []any{"component", "agent", "backend", name}
	for k, v := range fields {
		args = append(args, k, v)
	}
	slog.Info("══════════════════════════════════════════════════════", "component", "agent")
	slog.Info("  AGENT BACKEND ACTIVE → "+name, args...)
	slog.Info("══════════════════════════════════════════════════════", "component", "agent")
}

func boolStr(b bool) string {
	if b {
		return "yes"
	}
	return "no"
}
