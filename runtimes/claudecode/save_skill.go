package claudecode

import (
	"log/slog"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// Skill authoring / installing / listing for Claude Code.
// The runtime is NOT restarted for any of these: Claude Code discovers skills per session, the same
// contract the skill watcher relies on.

// SaveSkill writes a user-authored skill as <name>/SKILL.md.
func (s *ClaudeCodeService) SaveSkill(draft domain.SkillDraft) (string, error) {
	path, err := skills.WriteAuthoredSkill(claudecodeSkillsDir, draft.Name, draft.Description, draft.Instructions)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] authored skill saved", "component", "claudecode", "skill", draft.Name, "path", path)
	return path, nil
}

// InstallSkillArchive extracts a downloaded `.skill` bundle into the skills
// dir, replacing any existing skill of that name.
func (s *ClaudeCodeService) InstallSkillArchive(archivePath, fallbackName string) (string, error) {
	dir, count, err := skills.InstallSkillArchive(archivePath, claudecodeSkillsDir, fallbackName)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] archive installed", "component", "claudecode", "dir", dir, "files", count)
	return dir, nil
}

// ListSkills returns what is currently installed in the skills dir.
func (s *ClaudeCodeService) ListSkills() ([]domain.InstalledSkill, error) {
	return skills.ListInstalled(claudecodeSkillsDir)
}

// ReadSkillFiles returns one installed skill's files with text inlined, for the
// Manage-skills detail view.
func (s *ClaudeCodeService) ReadSkillFiles(name string) ([]domain.SkillBundleFile, error) {
	return skills.ReadSkillFiles(claudecodeSkillsDir, name)
}
func (s *ClaudeCodeService) ExportSkillArchive(name, destDir string) (string, error) {
	return skills.ExportSkillArchive(claudecodeSkillsDir, name, destDir)
}

func (s *ClaudeCodeService) ReadSkillFile(name, filePath string) (domain.SkillBundleFile, error) {
	return skills.ReadSkillFile(claudecodeSkillsDir, name, filePath)
}

// DeleteSkill removes an installed skill and returns the directory it deleted.
func (s *ClaudeCodeService) DeleteSkill(name string) (string, error) {
	path, err := skills.DeleteSkill(claudecodeSkillsDir, name)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] uninstalled", "component", "claudecode", "skill", name, "path", path)
	return path, nil
}

// InstallSkillMarkdown installs a bare SKILL.md; its front-matter names the skill.
func (s *ClaudeCodeService) InstallSkillMarkdown(content []byte) (string, error) {
	dir, err := skills.InstallSkillMarkdown(claudecodeSkillsDir, content)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] markdown installed", "component", "claudecode", "dir", dir)
	return dir, nil
}
