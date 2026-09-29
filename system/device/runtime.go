package device

import (
	"bytes"
	_ "embed"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"strings"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// switchRuntimeScript is the backend-agnostic switcher, embedded so it ships
// and OTA-updates with os-server.
//
//go:embed switch_runtime.sh
var switchRuntimeScript []byte

const switchRuntimeBin = "/usr/local/bin/switch-runtime"

// ensureSwitchRuntime writes the embedded switcher to switchRuntimeBin when missing or stale.
func ensureSwitchRuntime() error {
	if cur, err := os.ReadFile(switchRuntimeBin); err == nil && bytes.Equal(cur, switchRuntimeScript) {
		return nil
	}
	if err := os.WriteFile(switchRuntimeBin, switchRuntimeScript, 0o755); err != nil {
		return fmt.Errorf("write %s: %w", switchRuntimeBin, err)
	}
	return nil
}

// frDefaultAgentPath holds a build-baked per-image default runtime; it lives
// outside config.json so it survives Factory Reset. Usually absent.
var frDefaultAgentPath = "/root/config/f_r_default_agent"

// readFRDefaultAgent returns the baked per-image default runtime, or "".
func readFRDefaultAgent() string {
	b, err := os.ReadFile(frDefaultAgentPath)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(b))
}

// ResolveDefaultAgent returns the default runtime and its source:
// f_r_default_agent first, then ROBOT.md gateway.default, else ("", "").
// Single source of truth for seeding and agent.resolveRuntime; they must not
// resolve independently or config.json and the running gateway can disagree.
func ResolveDefaultAgent(cfg *config.Config) (value, source string) {
	if g := strings.ToLower(readFRDefaultAgent()); g != "" && domain.IsValidAgentRuntime(g) {
		return g, "f_r_default_agent"
	}
	if g := strings.ToLower(strings.TrimSpace(GatewayDefault(cfg.DeviceTypeOrDefault()))); g != "" && domain.IsValidAgentRuntime(g) {
		return g, "ROBOT.md gateway.default"
	}
	return "", ""
}

// SeedAgentRuntimeFromGateway persists ResolveDefaultAgent into an empty
// config.agent_runtime; an existing value is never touched.
func SeedAgentRuntimeFromGateway(cfg *config.Config) {
	if cfg == nil || strings.TrimSpace(cfg.AgentRuntime) != "" {
		return
	}
	g, _ := ResolveDefaultAgent(cfg)
	if g == "" {
		return
	}
	cfg.AgentRuntime = g
	if err := cfg.Save(); err != nil {
		slog.Error("seed agent_runtime from gateway.default failed", "component", "device", "runtime", g, "error", err)
	}
}

// CurrentAgentRuntime returns the effective agentic backend.
func (s *Service) CurrentAgentRuntime() string {
	return CurrentAgentRuntimeFromConfig(s.config)
}

// CurrentAgentRuntimeFromConfig returns config.agent_runtime, else
// ResolveDefaultAgent, else openclaw (same precedence as agent.resolveRuntime).
func CurrentAgentRuntimeFromConfig(cfg *config.Config) string {
	if r := strings.ToLower(strings.TrimSpace(cfg.AgentRuntime)); r != "" {
		return r
	}
	if g, _ := ResolveDefaultAgent(cfg); g != "" {
		return g
	}
	return domain.AgentRuntimeOpenClaw
}

// ReserveAgentRuntimeSwitch exclusively reserves the switcher and returns the
// switch function; call it exactly once (it releases the reservation).
func (s *Service) ReserveAgentRuntimeSwitch(d domain.AgentRuntimeSetData) (run func() (bool, error), err error) {
	return s.reserveAgentRuntimeSwitch(d, false)
}

// ReserveAgentRuntimeSwitchReady is ReserveAgentRuntimeSwitch that also requires
// the target's readiness probe to pass before persisting.
func (s *Service) ReserveAgentRuntimeSwitchReady(d domain.AgentRuntimeSetData) (run func() (bool, error), err error) {
	return s.reserveAgentRuntimeSwitch(d, true)
}

func (s *Service) reserveAgentRuntimeSwitch(d domain.AgentRuntimeSetData, waitReady bool) (run func() (bool, error), err error) {
	if !s.runtimeSwitchMu.TryLock() {
		return nil, ErrAgentRuntimeSwitchInProgress
	}
	return func() (bool, error) {
		defer s.runtimeSwitchMu.Unlock()
		return s.updateAgentRuntime(d, waitReady)
	}, nil
}

// updateAgentRuntime runs switch-runtime synchronously and persists
// config.agent_runtime only if it landed. Returns (true, nil) when switched:
// the caller must ack, then call RestartForAgentRuntime.
func (s *Service) updateAgentRuntime(d domain.AgentRuntimeSetData, waitReady bool) (bool, error) {
	runtime := strings.ToLower(strings.TrimSpace(d.Runtime))
	if !domain.IsValidAgentRuntime(runtime) {
		return false, fmt.Errorf("invalid runtime %q (want %s)", d.Runtime, strings.Join(domain.AgentRuntimes, "|"))
	}
	// "remote" needs a URL, so only the HTTP settings API can set it.
	if runtime == domain.AgentRuntimeRemote {
		return false, fmt.Errorf("runtime %q is only settable via the HTTP settings API (it needs the gateway URL)", runtime)
	}

	old := strings.ToLower(strings.TrimSpace(s.config.AgentRuntime))
	if old == "" {
		old = domain.AgentRuntimeOpenClaw
	}

	if old == runtime {
		slog.Info("agent runtime unchanged, skipping switch", "component", "device", "runtime", runtime)
		return false, nil
	}

	if err := ensureSwitchRuntime(); err != nil {
		return false, fmt.Errorf("install switch-runtime: %w", err)
	}
	if err := materializeInstaller(runtime); err != nil {
		return false, fmt.Errorf("materialize %s installer: %w", runtime, err)
	}
	// Non-fatal: a missing presync must not block the switch.
	if err := materializePresync(runtime); err != nil {
		slog.Warn("materialize presync hook failed (non-fatal)", "component", "device", "runtime", runtime, "error", err)
	}
	if waitReady {
		if err := materializeReadiness(runtime); err != nil {
			return false, fmt.Errorf("materialize %s readiness probe: %w", runtime, err)
		}
	}

	slog.Info("running switch-runtime", "component", "device", "from", old, "to", runtime)

	// Do not persist before the switch lands: config.json stays at `old` on
	// failure or crash, and switch-runtime has already rolled units back.
	if err := s.runSwitchRuntime(runtime, old, waitReady); err != nil {
		return false, fmt.Errorf("switch to %s failed, rolled back to %s: %w", runtime, old, err)
	}

	// On save failure the units are on NEW but disk says `old`; surface it so
	// the caller skips the restart.
	if err := s.config.WithLockSave(func(c *config.Config) {
		c.AgentRuntime = runtime
	}); err != nil {
		return false, fmt.Errorf("switch to %s landed but persisting agent_runtime failed: %w", runtime, err)
	}

	slog.Info("agent runtime switch landed", "component", "device", "from", old, "to", runtime)
	return true, nil
}

// runSwitchRuntime runs the switcher in a transient systemd unit and waits for
// its exit code (landed vs rolled back).
func (s *Service) runSwitchRuntime(newRuntime, oldRuntime string, waitReady bool) error {
	args := []string{"--quiet", "--collect", "--wait", "--unit=os-runtime-switch", switchRuntimeBin, newRuntime, oldRuntime}
	if waitReady {
		args = append(args, "--wait-ready")
	}
	if err := exec.Command("systemd-run", args...).Run(); err != nil {
		return fmt.Errorf("switch-runtime exited non-zero (see `journalctl -u os-runtime-switch`): %w", err)
	}
	return nil
}

// RestartForAgentRuntime restarts os-server via its own systemd-run unit (an
// inline restart would kill the child with our cgroup). Callers must ack first:
// this kills os-server.
func (s *Service) RestartForAgentRuntime() error {
	if err := exec.Command("systemd-run", "--collect", "--unit=os-server-runtime-restart",
		"systemctl", "restart", "os-server").Run(); err != nil {
		return fmt.Errorf("spawn os-server restart: %w", err)
	}
	return nil
}
