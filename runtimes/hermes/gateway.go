package hermes

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

const (
	hermesBinary = "/usr/local/bin/hermes"
	// switchRuntimeDir holds the unit-name + verify hook switch-runtime reads to
	// decide which unit to enable and whether hermes is already installed. install.sh
	// writes these; ensureGatewayUnit mirrors them when it installs the unit itself.
	switchRuntimeDir           = "/usr/local/lib/os-runtimes/hermes"
	gatewayHardwareStartupPath = "/etc/systemd/system/hermes-gateway.service.d/20-hardware-startup.conf"
)

// gatewayVerifyHook is the switch-runtime verify hook (mirrors install.sh): a cheap
// offline check so switch-runtime skips reinstall when hermes is already present.
const gatewayVerifyHook = "#!/usr/bin/env bash\ncommand -v hermes >/dev/null 2>&1\n"

// gatewayUnitExists reports whether the hermes-gateway systemd unit is known to
// systemd. `systemctl cat` reads the unit file from disk and exits non-zero when no
// such unit exists, so it answers "is the unit installed" without probing the live
// daemon (and works even while the daemon is down).
func gatewayUnitExists() bool {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return exec.CommandContext(ctx, "systemctl", "cat", hermesGatewayUnit).Run() == nil
}

// gatewayActive reports whether the hermes-gateway unit is currently running.
func gatewayActive() bool {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return exec.CommandContext(ctx, "systemctl", "is-active", "--quiet", hermesGatewayUnit).Run() == nil
}

// ensureGatewayUnit creates the hermes-gateway.service unit when it is absent and
// returns true when it installed it just now.
//
// Why this is needed (the A+B self-heal): the OrangePi image pre-bakes only the
// Hermes CLI *binary*; the gateway systemd unit is normally created by
// switch-runtime/install.sh on the first switch to hermes. A device that reaches
// hermes WITHOUT that switch — e.g. an operator hand-editing config.json's
// agent_runtime to "hermes" after a factory reset — therefore has a working
// `hermes --version` but no hermes-gateway.service. IsReady()/setup wait on the
// gateway's HTTP /health, so with no unit the gateway never starts, WaitForAgentReady
// times out, SetUpCompleted stays false, the device falls back to AP mode, and the
// symptom reads as "WiFi won't connect". Installing the unit on demand here closes
// that gap. It is fast: the binary + venv are already pre-baked, so `gateway install`
// only writes the unit file (no git clone / uv sync). The build-orangepi.sh unit
// pre-bake (A) makes this a no-op on freshly imaged devices; this (B) is the runtime
// backstop for config-flip / older-image / wiped-unit cases.
func (s *HermesService) ensureGatewayUnit() bool {
	if gatewayUnitExists() {
		ensureGatewayHardwareStartup()
		return false
	}
	slog.Warn("hermes onboarding: gateway unit absent — installing now",
		"component", "hermes", "unit", hermesGatewayUnit,
		"hint", "agent_runtime=hermes set without running switch-runtime/install.sh")

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	// `hermes gateway install` prompts [Y/n]; `yes` auto-answers. pipefail is off so
	// the broken pipe when `yes` outlives the install does not mask a real success
	// (same reason install.sh drops pipefail around this call).
	cmd := exec.CommandContext(ctx, "bash", "-c",
		"set +o pipefail; yes y | "+hermesBinary+" gateway install --system --run-as-user root")
	out, err := cmd.CombinedOutput()
	if len(out) > 0 {
		slog.Info("hermes gateway install output", "component", "hermes", "output", strings.TrimSpace(string(out)))
	}
	if err != nil {
		slog.Warn("hermes gateway install failed", "component", "hermes", "error", err)
		return false
	}
	if !gatewayUnitExists() {
		slog.Warn("hermes gateway install ran but unit still absent", "component", "hermes")
		return false
	}
	ensureGatewayHardwareStartup()
	declareSwitchRuntimeUnit()
	slog.Info("hermes onboarding: gateway unit installed", "component", "hermes", "unit", hermesGatewayUnit)
	return true
}

// declareSwitchRuntimeUnit writes the unit-name + verify hook switch-runtime reads,
// so a later runtime switch enables the right unit and skips a redundant reinstall.
// Best-effort: a failure here never blocks the boot/setup path.
func declareSwitchRuntimeUnit() {
	if err := os.MkdirAll(switchRuntimeDir, 0o755); err != nil {
		slog.Warn("hermes: mkdir switch-runtime dir failed", "component", "hermes", "error", err)
		return
	}
	if err := os.WriteFile(switchRuntimeDir+"/service", []byte(hermesGatewayUnit+"\n"), 0o644); err != nil {
		slog.Warn("hermes: write switch-runtime service file failed", "component", "hermes", "error", err)
	}
	if err := os.WriteFile(switchRuntimeDir+"/verify", []byte(gatewayVerifyHook), 0o755); err != nil {
		slog.Warn("hermes: write switch-runtime verify hook failed", "component", "hermes", "error", err)
	}
}

// enableHermesGateway marks the unit to auto-start on boot. Factory reset disables
// it (reset.go step 4, "SetupAgent re-enables"), so onboarding must re-enable it or
// hermes would not come back after a reboot. Best-effort: restart still starts it
// for the current session even if enable fails.
func enableHermesGateway() {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if out, err := exec.CommandContext(ctx, "systemctl", "enable", hermesGatewayUnit).CombinedOutput(); err != nil {
		slog.Warn("hermes gateway enable failed", "component", "hermes",
			"error", err, "output", strings.TrimSpace(string(out)))
	}
}

// Append a pre-start check without replacing upstream gateway commands or other
// drop-ins. The leading dash keeps older OS binaries compatible (fail-open).
const gatewayHardwareStartupConfig = "[Service]\nExecStartPre=-/usr/local/bin/os-server --wait-hal-ready\n"

func ensureGatewayHardwareStartup() {
	err := writeGatewayHardwareStartup(gatewayHardwareStartupPath, func() error {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		out, err := exec.CommandContext(ctx, "systemctl", "daemon-reload").CombinedOutput()
		if err != nil {
			return fmt.Errorf("daemon-reload: %w: %s", err, strings.TrimSpace(string(out)))
		}
		return nil
	})
	if err != nil {
		slog.Warn("hermes hardware startup drop-in failed", "component", "hermes", "error", err)
	}
}

func writeGatewayHardwareStartup(path string, reload func() error) error {
	previous, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("read hardware startup drop-in: %w", err)
	}
	existed := err == nil
	content := []byte(gatewayHardwareStartupConfig)
	if bytes.Equal(previous, content) {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("create hardware startup directory: %w", err)
	}
	if err := writeGatewayDropIn(path, content); err != nil {
		return err
	}
	if err := reload(); err != nil {
		// Restore the on-disk state so a subsequent ensure retries daemon-reload.
		var restoreErr error
		if existed {
			restoreErr = writeGatewayDropIn(path, previous)
		} else {
			restoreErr = os.Remove(path)
		}
		if restoreErr != nil {
			return fmt.Errorf("reload hardware startup drop-in: %w (restore failed: %v)", err, restoreErr)
		}
		return fmt.Errorf("reload hardware startup drop-in: %w", err)
	}
	return nil
}

func writeGatewayDropIn(path string, content []byte) error {
	file, err := os.CreateTemp(filepath.Dir(path), ".hardware-startup-*")
	if err != nil {
		return fmt.Errorf("create hardware startup drop-in: %w", err)
	}
	defer os.Remove(file.Name())
	if err := file.Chmod(0o644); err != nil {
		file.Close()
		return err
	}
	if _, err := file.Write(content); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	if err := os.Rename(file.Name(), path); err != nil {
		return fmt.Errorf("replace hardware startup drop-in: %w", err)
	}
	return nil
}
