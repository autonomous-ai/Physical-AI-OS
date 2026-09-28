package picoclaw

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
	picoclawDataDir       = "/root/.picoclaw" // data dir (HOME=/root)
	picoclawBin           = "/usr/local/bin/picoclaw"
	picoclawUnit          = "picoclaw" // systemd unit name
	picoStopVerifyTimeout = 5 * time.Second
)

// ResetAgent is the PicoClaw factory-reset wipe, called on the active gateway by
// server/system/factoryreset.go.
// PicoClaw keeps nothing: config.json/.security.yml are regenerated from the project config.json by
// presync.sh on the next switch.
func (s *PicoclawService) ResetAgent() error {
	wipePicoclawState()
	return nil
}

// wipePicoclawState: stop+disable gateway → wipe /root/.picoclaw → re-onboard.
func wipePicoclawState() {
	// The gateway holds the data dir open, so it must be stopped before the wipe.
	log.Printf("[factory-reset/picoclaw] step 1/4 — systemctl stop picoclaw")
	if out, err := exec.Command("systemctl", "stop", picoclawUnit).CombinedOutput(); err != nil {
		log.Printf("[factory-reset/picoclaw] step 1/4 — stop error: %v — %s", err, strings.TrimSpace(string(out)))
	}
	if waitForPicoclawStop(picoclawUnit, picoStopVerifyTimeout) {
		log.Printf("[factory-reset/picoclaw] step 1/4 — confirmed inactive")
	} else {
		log.Printf("[factory-reset/picoclaw] step 1/4 — WARNING still active after %s", picoStopVerifyTimeout)
	}

	log.Printf("[factory-reset/picoclaw] step 2/4 — systemctl disable picoclaw")
	if out, err := exec.Command("systemctl", "disable", picoclawUnit).CombinedOutput(); err != nil {
		log.Printf("[factory-reset/picoclaw] step 2/4 — disable error: %v — %s", err, strings.TrimSpace(string(out)))
	}

	log.Printf("[factory-reset/picoclaw] step 3/4 — wiping %s", picoclawDataDir)
	osreset.WipePath("[factory-reset/picoclaw]", picoclawDataDir)

	log.Printf("[factory-reset/picoclaw] step 4/4 — picoclaw onboard")
	cmd := exec.Command(picoclawBin, "onboard")
	cmd.Env = append(envWithoutHome(), "HOME=/root")
	if out, err := cmd.CombinedOutput(); err != nil {
		log.Printf("[factory-reset/picoclaw] step 4/4 — onboard error: %v — %s (non-fatal)", err, strings.TrimSpace(string(out)))
	} else {
		log.Printf("[factory-reset/picoclaw] step 4/4 — onboard done")
	}
}

// envWithoutHome returns the environment with HOME dropped, so the caller's appended
// HOME is the single effective value (getenv returns the first match).
func envWithoutHome() []string {
	env := os.Environ()
	out := env[:0]
	for _, kv := range env {
		if strings.HasPrefix(kv, "HOME=") {
			continue
		}
		out = append(out, kv)
	}
	return out
}

// waitForPicoclawStop polls is-active until the unit is inactive or timeout elapses.
func waitForPicoclawStop(unit string, timeout time.Duration) bool {
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
