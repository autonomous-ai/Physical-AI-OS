package hermes

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/skills"
)

const skillWatchInterval = 5 * time.Minute

// StartSkillWatcher polls OTA metadata for per-skill version changes and keeps the
// device's OpenClaw-imported skills fresh UNDER HERMES too. Skills reach Hermes by
// being copied (not converted) into ~/.hermes/skills/openclaw-imports by `claw
// migrate`, so the same CDN <name>.zip OpenClaw downloads drops in here verbatim —
// the watcher just targets the openclaw-imports dir instead of the OpenClaw
// workspace.
//
// This deliberately mirrors runtimes/openclaw/skill_watcher.go: identical loop,
// shared CDN/extract/hash plumbing from system/skills. The only differences are
// the target dir and the notify path — keep the two files parallel so they are
// easy to diff.
func (s *HermesService) StartSkillWatcher(ctx context.Context) {

	slog.Info("skill watcher started", "component", "skill-watcher", "backend", "Hermes", "interval", skillWatchInterval)

	// Seed last known versions from current metadata so first poll doesn't re-notify.
	lastVersions := map[string]string{}
	if initial, err := skills.FetchSkillVersions(s.config.OTAMetadataURL); err == nil && initial != nil {
		lastVersions = initial
		slog.Info("skill watcher seeded versions", "component", "skill-watcher", "count", len(lastVersions))
	}

	ticker := time.NewTicker(skillWatchInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			slog.Info("skill watcher stopped", "component", "skill-watcher")
			return
		case <-ticker.C:
			remote, err := skills.FetchSkillVersions(s.config.OTAMetadataURL)
			if err != nil {
				slog.Info("skill watcher: fetch failed", "component", "skill-watcher", "error", err)
				continue
			}
			slog.Info("skill watcher: checked", "component", "skill-watcher", "skills", len(remote))

			// Gate to what this device supports so a CDN version bump never re-adds
			// a capability-pruned skill (e.g. servo-control on a motionless device).
			supported := map[string]bool{}
			for _, n := range s.supportedSkills() {
				supported[n] = true
			}
			var toUpdate []string
			pendingVersions := map[string]string{}
			for name, ver := range remote {
				if !supported[name] {
					continue
				}
				if ver != "" && ver != lastVersions[name] {
					toUpdate = append(toUpdate, name)
					pendingVersions[name] = ver
				}
			}
			if len(toUpdate) == 0 {
				continue
			}

			slog.Info("skill versions changed", "component", "skill-watcher", "skills", toUpdate)
			result := s.downloadSkillsByNameResult(toUpdate)
			for _, name := range result.applied {
				lastVersions[name] = pendingVersions[name]
			}
			s.notifySkillChanges(result.changed)
		}
	}
}

// supportedSkills resolves this device's capabilities from ROBOT.md and filters
// the platform skill catalog — identical gating to OpenClaw (skills.Supported is
// runtime-agnostic platform metadata; the backend is just another consumer).
func (s *HermesService) supportedSkills() []string {
	return skills.Supported(device.Capabilities(s.config.DeviceTypeOrDefault()))
}

// otaBaseURL derives the CDN base from the device's OTA metadata URL (config.json),
// minus "/ota/metadata.json". Returns "" when unconfigured so callers skip rather
// than hit a hardcoded URL. Mirrors OpenClaw's otaBaseURL.
func (s *HermesService) otaBaseURL() string {
	u := strings.TrimSpace(s.config.OTAMetadataURL)
	if u == "" {
		return ""
	}
	return strings.TrimSuffix(u, "/ota/metadata.json")
}

func (s *HermesService) skillsBaseURL() string {
	if base := s.otaBaseURL(); base != "" {
		return base + "/skills"
	}
	return ""
}

// downloadSkills reconciles every platform skill supported by this device from
// the CDN. EnsureOnboarding calls it on every boot/config reconcile; the
// content hash in downloadSkillsByName keeps an unchanged catalog quiet.
func (s *HermesService) downloadSkills() []string {
	return s.downloadSkillsByName(s.supportedSkills())
}

// downloadSkillsByName downloads specific skill zips from CDN and extracts each
// atomically into ~/.hermes/skills/openclaw-imports/<name> (where `claw migrate`
// puts OpenClaw-imported skills). Returns names of skills that actually changed on
// disk. Parallel to OpenClaw's downloadSkillsByName — only the target dir differs.
func (s *HermesService) downloadSkillsByName(names []string) []string {
	return s.downloadSkillsByNameResult(names).changed
}

type skillDownloadResult struct {
	changed []string
	applied []string
}

// downloadSkillsByNameResult reports successfully applied skills separately
// from skills whose content changed. The watcher advances a version only after
// download and extraction succeed, so transient CDN failures retry next poll.
func (s *HermesService) downloadSkillsByNameResult(names []string) skillDownloadResult {
	base := s.skillsBaseURL()
	if base == "" {
		slog.Info("skill download skipped: no ota_metadata_url configured", "component", "skill-watcher")
		return skillDownloadResult{}
	}
	skillsDir := filepath.Join(hermesHome, "skills", "openclaw-imports")
	result := skillDownloadResult{}
	for _, name := range names {
		url := fmt.Sprintf("%s/%s.zip", base, name)
		tmpZip, err := skills.DownloadToTempFile(url, "skill-*.zip")
		if err != nil {
			slog.Warn("skill zip download failed", "component", "skill-watcher", "skill", name, "error", err)
			continue
		}

		targetDir := filepath.Join(skillsDir, name)

		// Hash existing content before extract so we can detect a no-op update —
		// metadata version bumped but actual files would land identical.
		oldHash, _ := skills.FolderHash(targetDir)

		if err := skills.ExtractSkillZip(tmpZip, targetDir); err != nil {
			slog.Warn("skill extract failed", "component", "skill-watcher", "skill", name, "error", err)
			os.Remove(tmpZip)
			continue
		}
		os.Remove(tmpZip)
		result.applied = append(result.applied, name)

		newHash, _ := skills.FolderHash(targetDir)
		if oldHash != "" && oldHash == newHash {
			slog.Info("skill content unchanged after extract, skipping notify",
				"component", "skill-watcher", "skill", name)
			continue
		}
		result.changed = append(result.changed, name)
	}
	return result
}

// notifySkillChanges tells the Hermes agent to re-read the changed skills.
func (s *HermesService) notifySkillChanges(changedSkills []string) {
	if len(changedSkills) == 0 {
		return
	}
	msg := skills.SkillUpdatePrompt("skills/openclaw-imports", changedSkills)
	slog.Info("INBOUND from system → agent (skill update)",
		"component", "skill-watcher", "backend", "Hermes",
		"source", "skill_watcher", "changed", changedSkills)
	if _, err := s.SendSystemChatMessage(msg); err != nil {
		slog.Warn("notify agent failed", "component", "skill-watcher", "error", err)
	}
}
