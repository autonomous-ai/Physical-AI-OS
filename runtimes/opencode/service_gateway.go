package opencode

import (
	"context"
	"log/slog"
	"os"
	"os/exec"
	"strings"
	"time"
)

// gatewayRestartTimeout bounds a single `systemctl restart opencode`.
const gatewayRestartTimeout = 60 * time.Second

// restartOpenCodeGateway restarts the opencode systemd unit (the bridge),
// which in turn respawns the OpenCode child — the only way to make Claude
// re-read CLAUDE.md / SOUL.md / .env / .mcp.json, all of which are loaded at
// session start.
func restartOpenCodeGateway() error {
	ctx, cancel := context.WithTimeout(context.Background(), gatewayRestartTimeout)
	defer cancel()

	if os.Geteuid() == 0 {
		if _, err := exec.LookPath("systemctl"); err == nil {
			out, err := exec.CommandContext(ctx, "systemctl", "restart", "opencode").CombinedOutput()
			if err == nil {
				return nil
			}
			slog.Warn("systemctl restart opencode failed", "component", "opencode-onboarding",
				"output", strings.TrimSpace(string(out)))
		}
	}
	slog.Warn("no systemctl restart available — skipping opencode gateway restart (changes apply on next start)",
		"component", "opencode-onboarding")
	return nil
}
