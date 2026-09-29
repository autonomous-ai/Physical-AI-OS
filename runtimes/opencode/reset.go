package opencode

import (
	"log"
	"os"
	"os/exec"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/osreset"
)

// Paths + unit set up by install.sh (see install.sh).
const (
	opencodeDataDir           = "/root/.opencode"             // bridge state dir (HOME=/root)
	opencodeXDGConfigDir      = "/root/.config/opencode"      // CLI global config (opencode.json, AGENTS.md, skills/)
	opencodeXDGDataDir        = "/root/.local/share/opencode" // CLI data (auth.json, sessions)
	opencodeUnit              = "opencode"                    // systemd unit name
	opencodeStopVerifyTimeout = 5 * time.Second
)

// ResetAgent is the OpenCode factory-reset wipe, called on the active gateway by
// server/system/factoryreset.go.
func (s *OpenCodeService) ResetAgent() error {
	wipeOpenCodeState()
	return nil
}

// wipeOpenCodeState: stop+disable gateway → wipe /root/.opencode → recreate baseline dirs.
func wipeOpenCodeState() {
	// The gateway holds the data dir open, so it must be stopped before the wipe.
	log.Printf("[factory-reset/opencode] step 1/4 — systemctl stop opencode")
	if out, err := exec.Command("systemctl", "stop", opencodeUnit).CombinedOutput(); err != nil {
		log.Printf("[factory-reset/opencode] step 1/4 — stop error: %v — %s", err, strings.TrimSpace(string(out)))
	}
	if waitForOpenCodeStop(opencodeUnit, opencodeStopVerifyTimeout) {
		log.Printf("[factory-reset/opencode] step 1/4 — confirmed inactive")
	} else {
		log.Printf("[factory-reset/opencode] step 1/4 — WARNING still active after %s", opencodeStopVerifyTimeout)
	}

	log.Printf("[factory-reset/opencode] step 2/4 — systemctl disable opencode")
	if out, err := exec.Command("systemctl", "disable", opencodeUnit).CombinedOutput(); err != nil {
		log.Printf("[factory-reset/opencode] step 2/4 — disable error: %v — %s", err, strings.TrimSpace(string(out)))
	}

	for _, d := range []string{opencodeDataDir, opencodeXDGConfigDir, opencodeXDGDataDir} {
		log.Printf("[factory-reset/opencode] step 3/4 — wiping %s", d)
		osreset.WipePath("[factory-reset/opencode]", d)
	}

	log.Printf("[factory-reset/opencode] step 4/4 — recreate baseline dirs")
	for _, d := range []string{opencodeDataDir + "/workspace", opencodeDataDir + "/attachments"} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			log.Printf("[factory-reset/opencode] step 4/4 — mkdir %s error: %v (non-fatal)", d, err)
		}
	}
}

// waitForOpenCodeStop polls is-active until the unit is inactive or timeout elapses.
func waitForOpenCodeStop(unit string, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for {
		if exec.Command("systemctl", "is-active", "--quiet", unit).Run() != nil {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(200 * time.Millisecond)
	}
}
