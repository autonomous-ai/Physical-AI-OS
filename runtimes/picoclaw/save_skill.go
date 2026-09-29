package picoclaw

import (
	"log/slog"
	"path/filepath"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// Skill authoring / installing / listing for PicoClaw.
// The runtime is NOT restarted for any of these: PicoClaw picks skills up per session, the same
// contract its skill watcher relies on.

// SaveSkill writes a user-authored skill as <name>/SKILL.md.
func (s *PicoclawService) SaveSkill(draft domain.SkillDraft) (string, error) {
	path, err := skills.WriteAuthoredSkill(picoclawSkillsDir(), draft.Name, draft.Description, draft.Instructions)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] authored skill saved", "component", "picoclaw", "skill", draft.Name, "path", path)
	return path, nil
}

// InstallSkillArchive extracts a downloaded `.skill` bundle into the skills
// dir, replacing any existing skill of that name.
func (s *PicoclawService) InstallSkillArchive(archivePath, fallbackName string) (string, error) {
	dir, count, err := skills.InstallSkillArchive(archivePath, picoclawSkillsDir(), fallbackName)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] archive installed", "component", "picoclaw", "dir", dir, "files", count)
	return dir, nil
}

// ListSkills returns what is currently installed in the skills dir.
func (s *PicoclawService) ListSkills() ([]domain.InstalledSkill, error) {
	return skills.ListInstalled(picoclawSkillsDir())
}

// picoclawSkillsDir is the tree PicoClaw loads skills from — the same one
// skill_watcher.go extracts CDN updates into.
func picoclawSkillsDir() string {
	return filepath.Join(picoclawWorkspaceDir, "skills")
}

// ReadSkillFiles returns one installed skill's files with text inlined, for the
// Manage-skills detail view.
func (s *PicoclawService) ReadSkillFiles(name string) ([]domain.SkillBundleFile, error) {
	return skills.ReadSkillFiles(picoclawSkillsDir(), name)
}
func (s *PicoclawService) ExportSkillArchive(name, destDir string) (string, error) {
	return skills.ExportSkillArchive(picoclawSkillsDir(), name, destDir)
}

func (s *PicoclawService) ReadSkillFile(name, filePath string) (domain.SkillBundleFile, error) {
	return skills.ReadSkillFile(picoclawSkillsDir(), name, filePath)
}

// DeleteSkill removes an installed skill and returns the directory it deleted.
func (s *PicoclawService) DeleteSkill(name string) (string, error) {
	path, err := skills.DeleteSkill(picoclawSkillsDir(), name)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] uninstalled", "component", "picoclaw", "skill", name, "path", path)
	return path, nil
}

// InstallSkillMarkdown installs a bare SKILL.md; its front-matter names the skill.
func (s *PicoclawService) InstallSkillMarkdown(content []byte) (string, error) {
	dir, err := skills.InstallSkillMarkdown(picoclawSkillsDir(), content)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] markdown installed", "component", "picoclaw", "dir", dir)
	return dir, nil
}
