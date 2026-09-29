package domain

import "testing"

func TestSummarizeSkills(t *testing.T) {
	got := SummarizeSkills([]InstalledSkill{
		{
			Name:        "music",
			Description: "Play music.",
			Files:       []SkillNode{{Name: "SKILL.md", Path: "music/SKILL.md"}},
		},
		{Name: "voice"},
	})

	if len(got) != 2 {
		t.Fatalf("want 2, got %d: %+v", len(got), got)
	}
	if got[0] != (SkillSummary{Name: "music", Description: "Play music."}) {
		t.Errorf("got[0] = %+v", got[0])
	}
	if got[1] != (SkillSummary{Name: "voice"}) {
		t.Errorf("got[1] = %+v", got[1])
	}
}

// TestSummarizeSkillsEmptyIsNil checks empty input yields nil so `skills` is omitted.
func TestSummarizeSkillsEmptyIsNil(t *testing.T) {
	if got := SummarizeSkills(nil); got != nil {
		t.Errorf("nil input: got %+v, want nil", got)
	}
	if got := SummarizeSkills([]InstalledSkill{}); got != nil {
		t.Errorf("empty input: got %+v, want nil", got)
	}
}
