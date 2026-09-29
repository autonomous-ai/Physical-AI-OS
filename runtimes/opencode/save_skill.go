package opencode

import (
	"log/slog"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// Skill authoring / installing / listing for OpenCode.
// The runtime is NOT restarted for any of these: OpenCode discovers skills per session, the same
// contract the skill watcher relies on.

// SaveSkill writes a user-authored skill as <name>/SKILL.md.
func (s *OpenCodeService) SaveSkill(draft domain.SkillDraft) (string, error) {
	path, err := skills.WriteAuthoredSkill(opencodeSkillsDir, draft.Name, draft.Description, draft.Instructions)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] authored skill saved", "component", "opencode", "skill", draft.Name, "path", path)
	return path, nil
}

// InstallSkillArchive extracts a downloaded `.skill` bundle into the skills
// dir, replacing any existing skill of that name.
func (s *OpenCodeService) InstallSkillArchive(archivePath, fallbackName string) (string, error) {
	dir, count, err := skills.InstallSkillArchive(archivePath, opencodeSkillsDir, fallbackName)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] archive installed", "component", "opencode", "dir", dir, "files", count)
	return dir, nil
}

// ListSkills returns what is currently installed in the skills dir.
func (s *OpenCodeService) ListSkills() ([]domain.InstalledSkill, error) {
	return skills.ListInstalled(opencodeSkillsDir)
}

// ReadSkillFiles returns one installed skill's files with text inlined, for the
// Manage-skills detail view.
func (s *OpenCodeService) ReadSkillFiles(name string) ([]domain.SkillBundleFile, error) {
	return skills.ReadSkillFiles(opencodeSkillsDir, name)
}
func (s *OpenCodeService) ExportSkillArchive(name, destDir string) (string, error) {
	return skills.ExportSkillArchive(opencodeSkillsDir, name, destDir)
}

func (s *OpenCodeService) ReadSkillFile(name, filePath string) (domain.SkillBundleFile, error) {
	return skills.ReadSkillFile(opencodeSkillsDir, name, filePath)
}

// DeleteSkill removes an installed skill and returns the directory it deleted.
func (s *OpenCodeService) DeleteSkill(name string) (string, error) {
	path, err := skills.DeleteSkill(opencodeSkillsDir, name)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] uninstalled", "component", "opencode", "skill", name, "path", path)
	return path, nil
}

// InstallSkillMarkdown installs a bare SKILL.md; its front-matter names the skill.
func (s *OpenCodeService) InstallSkillMarkdown(content []byte) (string, error) {
	dir, err := skills.InstallSkillMarkdown(opencodeSkillsDir, content)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] markdown installed", "component", "opencode", "dir", dir)
	return dir, nil
}
