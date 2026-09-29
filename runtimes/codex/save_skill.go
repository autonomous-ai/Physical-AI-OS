package codex

import (
	"log/slog"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// Skill authoring / installing / listing for Codex.
// The runtime is NOT restarted for any of these: Codex discovers skills per session, the same
// contract the skill watcher relies on.

// SaveSkill writes a user-authored skill as <name>/SKILL.md.
func (s *CodexService) SaveSkill(draft domain.SkillDraft) (string, error) {
	path, err := skills.WriteAuthoredSkill(codexSkillsDir, draft.Name, draft.Description, draft.Instructions)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] authored skill saved", "component", "codex", "skill", draft.Name, "path", path)
	return path, nil
}

// InstallSkillArchive extracts a downloaded `.skill` bundle into the skills
// dir, replacing any existing skill of that name.
func (s *CodexService) InstallSkillArchive(archivePath, fallbackName string) (string, error) {
	dir, count, err := skills.InstallSkillArchive(archivePath, codexSkillsDir, fallbackName)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] archive installed", "component", "codex", "dir", dir, "files", count)
	return dir, nil
}

// ListSkills returns what is currently installed in the skills dir.
func (s *CodexService) ListSkills() ([]domain.InstalledSkill, error) {
	return skills.ListInstalled(codexSkillsDir)
}

// ReadSkillFiles returns one installed skill's files with text inlined, for the
// Manage-skills detail view.
func (s *CodexService) ReadSkillFiles(name string) ([]domain.SkillBundleFile, error) {
	return skills.ReadSkillFiles(codexSkillsDir, name)
}
func (s *CodexService) ExportSkillArchive(name, destDir string) (string, error) {
	return skills.ExportSkillArchive(codexSkillsDir, name, destDir)
}

func (s *CodexService) ReadSkillFile(name, filePath string) (domain.SkillBundleFile, error) {
	return skills.ReadSkillFile(codexSkillsDir, name, filePath)
}

// DeleteSkill removes an installed skill and returns the directory it deleted.
func (s *CodexService) DeleteSkill(name string) (string, error) {
	path, err := skills.DeleteSkill(codexSkillsDir, name)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] uninstalled", "component", "codex", "skill", name, "path", path)
	return path, nil
}

// InstallSkillMarkdown installs a bare SKILL.md; its front-matter names the skill.
func (s *CodexService) InstallSkillMarkdown(content []byte) (string, error) {
	dir, err := skills.InstallSkillMarkdown(codexSkillsDir, content)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] markdown installed", "component", "codex", "dir", dir)
	return dir, nil
}
