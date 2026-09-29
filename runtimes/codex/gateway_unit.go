package codex

import (
	"log/slog"
	"os"
	"os/exec"
	"strings"
)

const codexUnitName = "codex"
const codexUnitPath = "/etc/systemd/system/codex.service"

// codexUnitContent MUST stay in sync with the unit install.sh writes —
// two writers, one contract (cross-referenced in install.sh).
const codexUnitContent = `[Unit]
Description=Codex agent gateway (os-server codex-gatewayd driving Codex App Server)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Environment=HOME=/root
EnvironmentFile=-/root/.codex/.env
WorkingDirectory=/root/.codex
ExecStart=/usr/local/bin/os-server codex-gatewayd
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
`

// ensureGatewayUnit writes the systemd unit when it is missing.
func (s *CodexService) ensureGatewayUnit() bool {
	if os.Geteuid() != 0 {
		return false
	}
	if _, err := exec.LookPath("systemctl"); err != nil {
		return false
	}
	if _, err := os.Stat(codexUnitPath); err == nil {
		return false
	}
	if err := os.WriteFile(codexUnitPath, []byte(codexUnitContent), 0o644); err != nil {
		slog.Warn("write codex unit failed", "component", "codex", "error", err)
		return false
	}
	if out, err := exec.Command("systemctl", "daemon-reload").CombinedOutput(); err != nil {
		slog.Warn("systemctl daemon-reload failed", "component", "codex",
			"output", strings.TrimSpace(string(out)), "error", err)
	}
	slog.Info("codex unit installed (self-heal)", "component", "codex", "path", codexUnitPath)
	return true
}

// gatewayActive reports whether the codex unit is currently active.
func gatewayActive() bool {
	if _, err := exec.LookPath("systemctl"); err != nil {
		return true
	}
	return exec.Command("systemctl", "is-active", "--quiet", codexUnitName).Run() == nil
}

// enableCodexGateway re-enables the unit so the gatewayd survives a reboot —
// factory reset disables it, and a freshly self-healed unit is not enabled.
func enableCodexGateway() {
	if os.Geteuid() != 0 {
		return
	}
	if _, err := exec.LookPath("systemctl"); err != nil {
		return
	}
	if out, err := exec.Command("systemctl", "enable", codexUnitName).CombinedOutput(); err != nil {
		slog.Warn("systemctl enable codex failed", "component", "codex",
			"output", strings.TrimSpace(string(out)), "error", err)
	}
}
