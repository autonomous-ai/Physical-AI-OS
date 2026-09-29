package picoclaw

import (
	"context"
	"log/slog"
	"os"
	"os/exec"
	"strings"
	"time"
)

// gatewayRestartTimeout bounds a single `systemctl restart picoclaw`.
const gatewayRestartTimeout = 60 * time.Second

// restartPicoclawGateway restarts the picoclaw systemd unit so the gateway fully
// re-reads its workspace + config.
// Called after EnsureOnboarding rewrites an OS-managed workspace block (SOUL/AGENTS/HEARTBEAT).
func restartPicoclawGateway() error {
	ctx, cancel := context.WithTimeout(context.Background(), gatewayRestartTimeout)
	defer cancel()

	if os.Geteuid() == 0 {
		if _, err := exec.LookPath("systemctl"); err == nil {
			out, err := exec.CommandContext(ctx, "systemctl", "restart", "picoclaw").CombinedOutput()
			if err == nil {
				return nil
			}
			slog.Warn("systemctl restart picoclaw failed", "component", "picoclaw-onboarding",
				"output", strings.TrimSpace(string(out)))
		}
	}
	slog.Warn("no systemctl restart available — skipping picoclaw gateway restart (changes apply on next start)",
		"component", "picoclaw-onboarding")
	return nil
}
