package claudecode

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

// StartSkillWatcher polls OTA metadata for per-skill version changes and auto-updates
// the workspace skills from the CDN. Mirrors runtimes/openclaw/skill_watcher.go: the
// CDN fetch / atomic extract / content-hash plumbing is runtime-agnostic and lives in
// system/skills; this file holds only the claudecode-specific loop, target dir, and
// notify. Capability-gated so a CDN version bump never re-adds a skill this device
// type does not support.
func (s *ClaudeCodeService) StartSkillWatcher(ctx context.Context) {
	slog.Info("skill watcher started", "component", "skill-watcher", "backend", "Claude Code", "interval", skillWatchInterval)

	// Seed last known versions so the first poll doesn't re-notify.
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

// supportedSkills resolves this device's capabilities from ROBOT.md and filters the
// platform skill catalog to what it can run (shared metadata in system/skills, the
// same gate OpenClaw uses).
func (s *ClaudeCodeService) supportedSkills() []string {
	return skills.Supported(device.Capabilities(s.config.DeviceTypeOrDefault()))
}

// otaBaseURL derives the CDN base from the device's OTA metadata URL
// (`<base>/ota/metadata.json`). Returns "" when unset so callers skip rather than
// fall back to a hardcoded URL. Mirrors openclaw.
func (s *ClaudeCodeService) otaBaseURL() string {
	u := strings.TrimSpace(s.config.OTAMetadataURL)
	if u == "" {
		return ""
	}
	return strings.TrimSuffix(u, "/ota/metadata.json")
}

func (s *ClaudeCodeService) skillsBaseURL() string {
	if base := s.otaBaseURL(); base != "" {
		return base + "/skills"
	}
	return ""
}

// downloadSkills reconciles every platform skill supported by this device from
// the CDN. EnsureOnboarding calls it on every boot/config reconcile; the
// content hash in downloadSkillsByName keeps an unchanged catalog quiet.
func (s *ClaudeCodeService) downloadSkills() []string {
	return s.downloadSkillsByName(s.supportedSkills())
}

// downloadSkillsByName downloads specific skill zips from the CDN, extracts each
// atomically into claudecodeSkillsDir/<name> (Claude Code's USER-level skill dir,
// auto-discovered in every session regardless of cwd), and returns the names that
// actually changed on disk (version pre-filter + content hash). Mirrors openclaw.
func (s *ClaudeCodeService) downloadSkillsByName(names []string) []string {
	return s.downloadSkillsByNameResult(names).changed
}

type skillDownloadResult struct {
	changed []string
	applied []string
}

// downloadSkillsByNameResult reports successfully applied skills separately
// from skills whose content changed. The watcher advances a version only after
// download and extraction succeed, so transient CDN failures retry next poll.
func (s *ClaudeCodeService) downloadSkillsByNameResult(names []string) skillDownloadResult {
	base := s.skillsBaseURL()
	if base == "" {
		slog.Info("skill download skipped: no ota_metadata_url configured", "component", "skill-watcher")
		return skillDownloadResult{}
	}
	skillsDir := claudecodeSkillsDir
	result := skillDownloadResult{}
	for _, name := range names {
		url := fmt.Sprintf("%s/%s.zip", base, name)
		tmpZip, err := skills.DownloadToTempFile(url, "skill-*.zip")
		if err != nil {
			slog.Warn("skill zip download failed", "component", "skill-watcher", "skill", name, "error", err)
			continue
		}

		targetDir := filepath.Join(skillsDir, name)
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

// notifySkillChanges tells the agent to re-read the changed skills. Mirrors openclaw.
func (s *ClaudeCodeService) notifySkillChanges(changedSkills []string) {
	if len(changedSkills) == 0 {
		return
	}
	msg := skills.SkillUpdatePrompt(claudecodeSkillsDir, changedSkills)
	slog.Info("INBOUND from system → agent (skill update)",
		"component", "skill-watcher", "backend", "Claude Code", "source", "skill_watcher", "changed", changedSkills)
	if _, err := s.SendSystemChatMessage(msg); err != nil {
		slog.Warn("notify agent failed", "component", "skill-watcher", "error", err)
	}
}
