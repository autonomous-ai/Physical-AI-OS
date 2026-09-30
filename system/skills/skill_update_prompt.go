package skills

import (
	"fmt"

	"go.autonomous.ai/os/system/domain"
)

// SkillUpdatePrompt builds the instruction to reload the changed skills from skillsDir.
func SkillUpdatePrompt(skillsDir string, changedSkills []string) string {
	list := ""
	for _, name := range changedSkills {
		list += fmt.Sprintf("\n- %s/%s/SKILL.md", skillsDir, name)
	}
	return "[system] The following skills have been updated. Re-read them now — files on disk have changed. Follow the updated instructions strictly. After re-reading, output exactly NO_REPLY and nothing else; do not announce or acknowledge this maintenance update to the user." + list +
		"\n" + domain.ViaMarker(domain.ViaSystem)
}
