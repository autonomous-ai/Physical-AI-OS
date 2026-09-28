package skills

import "fmt"

// SkillUpdatePrompt builds the shared instruction to reload updated skills. The
// caller supplies its agent-visible skills directory and handles delivery.
func SkillUpdatePrompt(skillsDir string, changedSkills []string) string {
	list := ""
	for _, name := range changedSkills {
		list += fmt.Sprintf("\n- %s/%s/SKILL.md", skillsDir, name)
	}
	return "[system] The following skills have been updated. Re-read them now — files on disk have changed. Follow the updated instructions strictly. After re-reading, output exactly NO_REPLY and nothing else; do not announce or acknowledge this maintenance update to the user." + list
}
