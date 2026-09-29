package openclaw

import (
	"log/slog"
	"path/filepath"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// SaveSkill writes a user-authored skill to workspace/skills/<name>/SKILL.md.
func (s *OpenclawService) SaveSkill(draft domain.SkillDraft) (string, error) {
	path, err := skills.WriteAuthoredSkill(s.skillsDir(), draft.Name, draft.Description, draft.Instructions)
	if err != nil {
		return "", err
	}

	slog.Info("[skills] authored skill saved", "component", "openclaw", "skill", draft.Name, "path", path)
	return path, nil
}

// InstallSkillArchive extracts a downloaded `.skill` bundle into the same workspace/skills tree, replacing any existing skill of that name.
func (s *OpenclawService) InstallSkillArchive(archivePath, fallbackName string) (string, error) {
	dir, count, err := skills.InstallSkillArchive(archivePath, s.skillsDir(), fallbackName)
	if err != nil {
		return "", err
	}

	slog.Info("[skills] archive installed", "component", "openclaw", "dir", dir, "files", count)
	return dir, nil
}

// ListSkills returns every skill installed in workspace/skills.
func (s *OpenclawService) ListSkills() ([]domain.InstalledSkill, error) {
	return skills.ListInstalled(s.skillsDir())
}

// skillsDir is the tree OpenClaw loads skills from — the same one InstallRoleSkills and EnsureMCPSkill write into.
func (s *OpenclawService) skillsDir() string {
	return filepath.Join(s.config.OpenclawConfigDir, "workspace", "skills")
}

// ReadSkillFiles returns one installed skill's files with text inlined, for the Manage-skills detail view.
func (s *OpenclawService) ReadSkillFiles(name string) ([]domain.SkillBundleFile, error) {
	return skills.ReadSkillFiles(s.skillsDir(), name)
}
func (s *OpenclawService) ExportSkillArchive(name, destDir string) (string, error) {
	return skills.ExportSkillArchive(s.skillsDir(), name, destDir)
}

func (s *OpenclawService) ReadSkillFile(name, filePath string) (domain.SkillBundleFile, error) {
	return skills.ReadSkillFile(s.skillsDir(), name, filePath)
}

// DeleteSkill removes an installed skill and returns the directory it deleted.
func (s *OpenclawService) DeleteSkill(name string) (string, error) {
	path, err := skills.DeleteSkill(s.skillsDir(), name)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] uninstalled", "component", "openclaw", "skill", name, "path", path)
	return path, nil
}

// InstallSkillMarkdown installs a bare SKILL.md; its front-matter names the skill.
func (s *OpenclawService) InstallSkillMarkdown(content []byte) (string, error) {
	dir, err := skills.InstallSkillMarkdown(s.skillsDir(), content)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] markdown installed", "component", "openclaw", "dir", dir)
	return dir, nil
}
