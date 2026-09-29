package codex

import (
	"context"
	"log/slog"
	"os"
	"os/exec"
	"strings"
	"time"
)

// gatewayRestartTimeout bounds a single `systemctl restart codex`.
const gatewayRestartTimeout = 60 * time.Second

// restartCodexGateway restarts the codex systemd unit (the bridge),
// which in turn respawns the Codex child — the only way to make Claude
// re-read CLAUDE.md / SOUL.md / .env / .mcp.json, all of which are loaded at
// session start.
func restartCodexGateway() error {
	ctx, cancel := context.WithTimeout(context.Background(), gatewayRestartTimeout)
	defer cancel()

	if os.Geteuid() == 0 {
		if _, err := exec.LookPath("systemctl"); err == nil {
			out, err := exec.CommandContext(ctx, "systemctl", "restart", "codex").CombinedOutput()
			if err == nil {
				return nil
			}
			slog.Warn("systemctl restart codex failed", "component", "codex-onboarding",
				"output", strings.TrimSpace(string(out)))
		}
	}
	slog.Warn("no systemctl restart available — skipping codex gateway restart (changes apply on next start)",
		"component", "codex-onboarding")
	return nil
}
