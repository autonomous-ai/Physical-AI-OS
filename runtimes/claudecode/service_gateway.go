package claudecode

import (
	"context"
	"log/slog"
	"os"
	"os/exec"
	"strings"
	"time"
)

// gatewayRestartTimeout bounds a single `systemctl restart claudecode`.
const gatewayRestartTimeout = 60 * time.Second

// restartClaudeCodeGateway restarts the claudecode systemd unit (the bridge),
// which in turn respawns the Claude Code child — the only way to make Claude
// re-read CLAUDE.md / SOUL.md / .env / .mcp.json, all of which are loaded at
// session start.
func restartClaudeCodeGateway() error {
	ctx, cancel := context.WithTimeout(context.Background(), gatewayRestartTimeout)
	defer cancel()

	if os.Geteuid() == 0 {
		if _, err := exec.LookPath("systemctl"); err == nil {
			out, err := exec.CommandContext(ctx, "systemctl", "restart", "claudecode").CombinedOutput()
			if err == nil {
				return nil
			}
			slog.Warn("systemctl restart claudecode failed", "component", "claudecode-onboarding",
				"output", strings.TrimSpace(string(out)))
		}
	}
	slog.Warn("no systemctl restart available — skipping claudecode gateway restart (changes apply on next start)",
		"component", "claudecode-onboarding")
	return nil
}
