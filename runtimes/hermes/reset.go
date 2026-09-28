package hermes

import (
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/lib/osreset"
	"go.autonomous.ai/os/system/server/config"
)

// ResetAgent performs the Hermes factory-reset wipe.
func (s *HermesService) ResetAgent() error {
	wipeHermesState(s.config)
	return nil
}

// stopVerifyTimeout caps how long we wait for hermes-gateway to actually leave the active state after `systemctl stop`.
const stopVerifyTimeout = 5 * time.Second

// isServiceActive returns true if `systemctl is-active <unit>` exits 0 (active).
func isServiceActive(unit string) bool {
	return exec.Command("systemctl", "is-active", "--quiet", unit).Run() == nil
}

// waitForServiceStop polls is-active until the unit is no longer active or the timeout elapses.
func waitForServiceStop(unit string, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for {
		if !isServiceActive(unit) {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(200 * time.Millisecond)
	}
}

// hermesWipeDirs are Hermes state subdirs we recursively remove on factory reset.
var hermesWipeDirs = []string{
	hermesHome + "/sessions",
	hermesHome + "/memories",
	hermesHome + "/tasks",
	hermesHome + "/subagents",
	hermesHome + "/checkpoints",
	hermesHome + "/logs",
	hermesHome + "/.cache",
	hermesHome + "/cron",
	hermesHome + "/cache",
	hermesHome + "/gateway",
	hermesHome + "/migration",
	hermesHome + "/skills/openclaw-imports",
	hermesHome + "/skills/audio_cache",
	hermesHome + "/skills/image_cache",
}

// hermesKeepFiles is the allow-list of TOP-LEVEL FILES (not dirs) under hermesHome that survive factory reset.
var hermesKeepFiles = map[string]bool{
	".env":        true,
	"config.yaml": true,
	"auth.lock":   true,
	"SOUL.md":     true,
}

// hermesSoulFallback is written to SOUL.md only when the device has no soul_ref and hermes setup --reset did not seed one.
const hermesSoulFallback = "# Hermes Agent Persona\n"

// wipeHermesState runs the Hermes reset flow.
// The daemon must be stopped first: it holds SQLite handles and re-creates wiped paths.
func wipeHermesState(cfg *config.Config) {
	log.Printf("[factory-reset/hermes] step 1/5 — hermes gateway stop")
	if out, err := exec.Command("hermes", "gateway", "stop").CombinedOutput(); err != nil {
		log.Printf("[factory-reset/hermes] step 1/5 — hermes gateway stop error: %v — %s", err, strings.TrimSpace(string(out)))
	} else {
		log.Printf("[factory-reset/hermes] step 1/5 — hermes gateway stopped")
	}

	log.Printf("[factory-reset/hermes] step 2/5 — systemctl stop hermes-gateway")
	if out, err := exec.Command("systemctl", "stop", "hermes-gateway").CombinedOutput(); err != nil {
		log.Printf("[factory-reset/hermes] step 2/5 — stop hermes-gateway error: %v — %s", err, strings.TrimSpace(string(out)))
	} else {
		log.Printf("[factory-reset/hermes] step 2/5 — hermes-gateway stop returned ok")
	}
	if waitForServiceStop("hermes-gateway", stopVerifyTimeout) {
		log.Printf("[factory-reset/hermes] step 2/5 — hermes-gateway confirmed inactive")
	} else {
		log.Printf("[factory-reset/hermes] step 2/5 — WARNING hermes-gateway still active after %s — SQLite wipe may race the running daemon",
			stopVerifyTimeout)
	}

	log.Printf("[factory-reset/hermes] step 3/5 — hermes setup --reset --non-interactive")
	if out, err := exec.Command("hermes", "setup", "--reset", "--non-interactive").CombinedOutput(); err != nil {
		log.Printf("[factory-reset/hermes] step 3/5 — hermes setup --reset error: %v — %s", err, strings.TrimSpace(string(out)))
	} else {
		log.Printf("[factory-reset/hermes] step 3/5 — hermes setup --reset done: %s", strings.TrimSpace(string(out)))
	}

	log.Printf("[factory-reset/hermes] step 4/5 — systemctl disable hermes-gateway")
	if out, err := exec.Command("systemctl", "disable", "hermes-gateway").CombinedOutput(); err != nil {
		log.Printf("[factory-reset/hermes] step 4/5 — disable hermes-gateway error: %v — %s", err, strings.TrimSpace(string(out)))
	} else {
		log.Printf("[factory-reset/hermes] step 4/5 — hermes-gateway disabled")
	}

	log.Printf("[factory-reset/hermes] step 5/5 — wiping %d dirs + top-level file sweep", len(hermesWipeDirs))

	for _, d := range hermesWipeDirs {
		osreset.WipePath("[factory-reset/hermes]", d)
	}

	entries, err := os.ReadDir(hermesHome)
	if err != nil {
		log.Printf("[factory-reset/hermes] sweep: cannot read %s: %v (non-fatal)", hermesHome, err)
	} else {
		swept := 0
		for _, e := range entries {
			if e.IsDir() {
				continue
			}
			if hermesKeepFiles[e.Name()] {
				continue
			}
			osreset.WipePath("[factory-reset/hermes]", filepath.Join(hermesHome, e.Name()))
			swept++
		}
		log.Printf("[factory-reset/hermes] sweep: removed %d top-level files (keep-list: %d)", swept, len(hermesKeepFiles))
	}

	// SOUL.md: kept by sweep, but user-customised content must not survive a factory reset.
	soulPath := filepath.Join(hermesHome, "SOUL.md")
	soulContent := resolveSoulContent(cfg)
	if err := os.WriteFile(soulPath, soulContent, 0o644); err != nil {
		log.Printf("[factory-reset/hermes] reset SOUL.md: %v (non-fatal)", err)
	} else {
		log.Printf("[factory-reset/hermes] reset SOUL.md (%d bytes)", len(soulContent))
	}
}

// resolveSoulContent returns the SOUL.md to seed after reset: the soul_ref content, else hermesSoulFallback.
func resolveSoulContent(cfg *config.Config) []byte {
	devType := cfg.DeviceTypeOrDefault()
	content, hasSoul, err := device.ResolveSoul(devType)
	if err != nil {
		log.Printf("[factory-reset/hermes] WARN soul_ref for %q unresolved: %v — using fallback", devType, err)
		return []byte(hermesSoulFallback)
	}
	if !hasSoul {
		return []byte(hermesSoulFallback)
	}
	log.Printf("[factory-reset/hermes] soul_ref seeded for %q (%d bytes)", devType, len(content))
	return content
}
