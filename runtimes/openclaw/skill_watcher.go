package openclaw

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"time"

	"go.autonomous.ai/os/system/skills"
)

const skillWatchInterval = 5 * time.Minute

// StartSkillWatcher polls OTA metadata for per-skill version changes.
// When any skill version changes, downloads that skill zip from CDN,
// extracts atomically, and notifies the agent to re-read it.
//
// The CDN fetch / atomic extract / content-hash plumbing is runtime-agnostic and
// lives in system/skills (FetchSkillVersions / DownloadToTempFile / FolderHash /
// ExtractSkillZip); this file holds only the OpenClaw-specific loop, target dir,
// and notify. runtimes/hermes/skill_watcher.go is its parallel under Hermes.
func (s *OpenclawService) StartSkillWatcher(ctx context.Context) {

	slog.Info("skill watcher started", "component", "skill-watcher", "interval", skillWatchInterval)

	// Seed last known versions from current metadata so first poll doesn't re-notify
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

			// Find skills with changed versions, gated to what this device
			// supports so a CDN version bump never re-adds a capability-pruned
			// skill (e.g. servo-control on a motionless device).
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

// downloadSkills downloads the skills this device supports from CDN (capability-
// gated via supportedSkills), returning names of changed ones.
func (s *OpenclawService) downloadSkills() []string {
	return s.downloadSkillsByName(s.supportedSkills())
}

// downloadSkillsByName downloads specific skill zips from CDN, extracts each
// atomically into workspace/skills/<name>, returns names of skills that actually
// changed on disk. Each skill is published as <name>.zip containing the whole
// skill folder; the version pre-filter + content hash mean a returned name had
// real content changes.
func (s *OpenclawService) downloadSkillsByName(names []string) []string {
	return s.downloadSkillsByNameResult(names).changed
}

type skillDownloadResult struct {
	changed []string
	applied []string
}

// downloadSkillsByNameResult reports successfully applied skills separately from
// skills whose content changed. A watcher only advances an OTA version after its
// archive is downloaded and extracted, so a transient failure is retried.
func (s *OpenclawService) downloadSkillsByNameResult(names []string) skillDownloadResult {
	base := s.skillsBaseURL()
	if base == "" {
		slog.Info("skill download skipped: no ota_metadata_url configured", "component", "skill-watcher")
		return skillDownloadResult{}
	}
	skillsDir := filepath.Join(s.config.OpenclawConfigDir, "workspace", "skills")
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

// notifySkillChanges sends a single message to the agent listing all changed skills.
func (s *OpenclawService) notifySkillChanges(changedSkills []string) {
	if len(changedSkills) == 0 {
		return
	}
	msg := skills.SkillUpdatePrompt("skills", changedSkills)
	slog.Info("INBOUND from system → agent (skill update)",
		"component", "skill-watcher", "backend", "OpenClaw",
		"source", "skill_watcher", "changed", changedSkills)
	if _, err := s.SendSystemChatMessage(msg); err != nil {
		slog.Warn("notify agent failed", "component", "skill-watcher", "error", err)
	}
}
