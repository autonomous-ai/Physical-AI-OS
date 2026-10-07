package openclaw

import (
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"go.autonomous.ai/os/system/lib/osreset"
)

// ResetAgent performs the OpenClaw factory-reset wipe.
func (s *OpenclawService) ResetAgent() error {
	wipeOpenclawState()
	return nil
}

// openclawStatePaths are state dirs `openclaw reset` leaves behind; factory reset wipes them manually.
var openclawStatePaths = []string{
	"/root/.openclaw/agents",
	"/root/.openclaw/workspace",
	"/root/.openclaw/workspace-attestations",
	// OpenClaw >= 2026.9 keeps the workspace attestation in state/openclaw.sqlite;
	// left behind, it blocks reseeding the wiped workspace for 24 h.
	"/root/.openclaw/state",
	"/root/.openclaw/devices",
	"/root/.openclaw/tasks",
	"/root/.openclaw/logs",
	"/root/.openclaw/telegram",
	"/root/.openclaw/discord",
	"/root/.openclaw/plugin-state",
	"/root/.openclaw/memory",
	"/root/.openclaw/delivery-queue",
	"/root/.openclaw/subagents",
	"/root/.openclaw/cron",
	"/root/.openclaw/media",
	"/root/.openclaw/flows",
	"/root/.openclaw/openclaw.json.last-good",
	"/root/.openclaw/update-check.json",
	"/root/.openclaw/.openclaw",
	"/root/.openclaw/.cache",
}

// wipeOpenclawState runs the 3-step OpenClaw reset: CLI reset → disable service → manual rm -rf of dirs the CLI doesn't touch.
func wipeOpenclawState() {
	log.Printf("[factory-reset/openclaw] step 1/3 — openclaw reset --scope config+creds+sessions")
	out, err := exec.Command("openclaw", "reset",
		"--scope", "config+creds+sessions",
		"--yes", "--non-interactive",
	).CombinedOutput()
	outStr := strings.TrimSpace(string(out))
	if err != nil {
		log.Printf("[factory-reset/openclaw] step 1/3 — openclaw reset error: %v — %s", err, outStr)
	} else {
		log.Printf("[factory-reset/openclaw] step 1/3 — openclaw reset done: %s", outStr)
	}

	log.Printf("[factory-reset/openclaw] step 2/3 — disabling openclaw.service")
	if out, err := exec.Command("systemctl", "disable", "openclaw").CombinedOutput(); err != nil {
		log.Printf("[factory-reset/openclaw] step 2/3 — disable openclaw error: %v — %s", err, strings.TrimSpace(string(out)))
	} else {
		log.Printf("[factory-reset/openclaw] step 2/3 — openclaw.service disabled")
	}

	if bakFiles, err := filepath.Glob("/root/.openclaw/openclaw.json.bak*"); err == nil {
		for _, f := range bakFiles {
			if err := os.Remove(f); err != nil {
				if os.IsNotExist(err) {
					continue
				}
				log.Printf("[factory-reset/openclaw] wipe %s: %v (non-fatal)", f, err)
				continue
			}
			log.Printf("[factory-reset/openclaw] wiped %s", f)
		}
	}
	log.Printf("[factory-reset/openclaw] step 3/3 — wiping %d openclaw state paths", len(openclawStatePaths))
	for _, p := range openclawStatePaths {
		osreset.WipePath("[factory-reset/openclaw]", p)
	}
}
