package codex

import (
	"log"
	"os"
	"os/exec"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/osreset"
)

// Paths + unit set up by install.sh (see install.sh).
var codexDataDir = codexHome // data dir ($CODEX_HOME)

const (
	codexUnit              = "codex" // systemd unit name
	codexStopVerifyTimeout = 5 * time.Second
)

// ResetAgent is the Codex factory-reset wipe, called on the active gateway by
// server/system/factoryreset.go.
func (s *CodexService) ResetAgent() error {
	wipeCodexState()
	return nil
}

// wipeCodexState: stop+disable gateway → wipe /root/.codex → recreate baseline dirs.
func wipeCodexState() {
	// The gateway holds the data dir open, so it must be stopped before the wipe.
	log.Printf("[factory-reset/codex] step 1/4 — systemctl stop codex")
	if out, err := exec.Command("systemctl", "stop", codexUnit).CombinedOutput(); err != nil {
		log.Printf("[factory-reset/codex] step 1/4 — stop error: %v — %s", err, strings.TrimSpace(string(out)))
	}
	if waitForCodexStop(codexUnit, codexStopVerifyTimeout) {
		log.Printf("[factory-reset/codex] step 1/4 — confirmed inactive")
	} else {
		log.Printf("[factory-reset/codex] step 1/4 — WARNING still active after %s", codexStopVerifyTimeout)
	}

	log.Printf("[factory-reset/codex] step 2/4 — systemctl disable codex")
	if out, err := exec.Command("systemctl", "disable", codexUnit).CombinedOutput(); err != nil {
		log.Printf("[factory-reset/codex] step 2/4 — disable error: %v — %s", err, strings.TrimSpace(string(out)))
	}

	log.Printf("[factory-reset/codex] step 3/4 — wiping %s", codexDataDir)
	osreset.WipePath("[factory-reset/codex]", codexDataDir)

	log.Printf("[factory-reset/codex] step 4/4 — recreate baseline dirs")
	for _, d := range []string{codexDataDir + "/workspace", codexDataDir + "/attachments"} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			log.Printf("[factory-reset/codex] step 4/4 — mkdir %s error: %v (non-fatal)", d, err)
		}
	}
}

// waitForCodexStop polls is-active until the unit is inactive or timeout elapses.
func waitForCodexStop(unit string, timeout time.Duration) bool {
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
