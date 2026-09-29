package hermes

import (
	"log/slog"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/skills"
)

// Skill authoring / installing / listing for Hermes.
const (
	// hermesAuthoredSkillsDir is where everything os-server writes lands.
	hermesAuthoredSkillsDir = hermesHome + "/skills/authored"

	// hermesImportedSkillsDir is the migrate-owned root, read-only from here.
	hermesImportedSkillsDir = hermesHome + "/skills/openclaw-imports"
)

// SaveSkill writes a user-authored skill as <name>/SKILL.md under the device-owned root.
func (s *HermesService) SaveSkill(draft domain.SkillDraft) (string, error) {
	path, err := skills.WriteAuthoredSkill(hermesAuthoredSkillsDir, draft.Name, draft.Description, draft.Instructions)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] authored skill saved", "component", "hermes", "skill", draft.Name, "path", path)
	return path, nil
}

// InstallSkillArchive extracts a downloaded `.skill` bundle into the device-owned root, replacing any existing skill of that name there.
func (s *HermesService) InstallSkillArchive(archivePath, fallbackName string) (string, error) {
	dir, count, err := skills.InstallSkillArchive(archivePath, hermesAuthoredSkillsDir, fallbackName)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] archive installed", "component", "hermes", "dir", dir, "files", count)
	return dir, nil
}

// ListSkills merges both roots.
func (s *HermesService) ListSkills() ([]domain.InstalledSkill, error) {
	return skills.ListInstalledFrom(hermesAuthoredSkillsDir, hermesImportedSkillsDir)
}

// ReadSkillFiles searches the device-owned root first, then the migrate-owned one.
func (s *HermesService) ReadSkillFiles(name string) ([]domain.SkillBundleFile, error) {
	return skills.ReadSkillFilesFrom(name, hermesAuthoredSkillsDir, hermesImportedSkillsDir)
}
func (s *HermesService) ExportSkillArchive(name, destDir string) (string, error) {
	return skills.ExportSkillArchive(hermesAuthoredSkillsDir, name, destDir)
}

func (s *HermesService) ReadSkillFile(name, filePath string) (domain.SkillBundleFile, error) {
	return skills.ReadSkillFileFrom(name, filePath, hermesAuthoredSkillsDir, hermesImportedSkillsDir)
}

// DeleteSkill removes the skill from whichever root has it, device-owned first — same precedence as ListSkills, so an uninstall hits the skill the listing showed.
func (s *HermesService) DeleteSkill(name string) (string, error) {
	path, err := skills.DeleteSkillFrom(name, hermesAuthoredSkillsDir, hermesImportedSkillsDir)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] uninstalled", "component", "hermes", "skill", name, "path", path)
	return path, nil
}

// InstallSkillMarkdown installs a bare SKILL.md; its front-matter names the skill.
func (s *HermesService) InstallSkillMarkdown(content []byte) (string, error) {
	dir, err := skills.InstallSkillMarkdown(hermesAuthoredSkillsDir, content)
	if err != nil {
		return "", err
	}
	slog.Info("[skills] markdown installed", "component", "hermes", "dir", dir)
	return dir, nil
}
