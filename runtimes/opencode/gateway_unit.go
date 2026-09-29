package opencode

import (
	"log/slog"
	"os"
	"os/exec"
	"strings"
)

const opencodeUnitName = "opencode"
const opencodeUnitPath = "/etc/systemd/system/opencode.service"

// opencodeUnitContent MUST stay in sync with the unit install.sh writes —
// two writers, one contract (cross-referenced in install.sh).
const opencodeUnitContent = `[Unit]
Description=OpenCode agent gateway (os-server opencode-gatewayd driving ` + "`opencode run`" + ` per turn)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=HOME=/root
EnvironmentFile=-/root/.opencode/.env
WorkingDirectory=/root/.opencode
ExecStart=/usr/local/bin/os-server opencode-gatewayd
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
`

// ensureGatewayUnit writes the systemd unit when it is missing.
func (s *OpenCodeService) ensureGatewayUnit() bool {
	if os.Geteuid() != 0 {
		return false
	}
	if _, err := exec.LookPath("systemctl"); err != nil {
		return false
	}
	if _, err := os.Stat(opencodeUnitPath); err == nil {
		return false
	}
	if err := os.WriteFile(opencodeUnitPath, []byte(opencodeUnitContent), 0o644); err != nil {
		slog.Warn("write opencode unit failed", "component", "opencode", "error", err)
		return false
	}
	if out, err := exec.Command("systemctl", "daemon-reload").CombinedOutput(); err != nil {
		slog.Warn("systemctl daemon-reload failed", "component", "opencode",
			"output", strings.TrimSpace(string(out)), "error", err)
	}
	slog.Info("opencode unit installed (self-heal)", "component", "opencode", "path", opencodeUnitPath)
	return true
}

// gatewayActive reports whether the opencode unit is currently active.
func gatewayActive() bool {
	if _, err := exec.LookPath("systemctl"); err != nil {
		return true
	}
	return exec.Command("systemctl", "is-active", "--quiet", opencodeUnitName).Run() == nil
}

// enableOpenCodeGateway re-enables the unit so the gatewayd survives a reboot —
// factory reset disables it, and a freshly self-healed unit is not enabled.
func enableOpenCodeGateway() {
	if os.Geteuid() != 0 {
		return
	}
	if _, err := exec.LookPath("systemctl"); err != nil {
		return
	}
	if out, err := exec.Command("systemctl", "enable", opencodeUnitName).CombinedOutput(); err != nil {
		slog.Warn("systemctl enable opencode failed", "component", "opencode",
			"output", strings.TrimSpace(string(out)), "error", err)
	}
}
