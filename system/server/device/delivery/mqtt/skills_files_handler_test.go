package mqtthandler

import (
	"strings"
	"testing"
	"unicode/utf8"

	"go.autonomous.ai/os/system/domain"
)

func sampleSkillFiles() []domain.SkillBundleFile {
	return []domain.SkillBundleFile{
		{Path: "music/SKILL.md", Size: 12, Text: "# Music"},
		{Path: "music/reference/tempo.md", Size: 5, Text: "tempo"},
		{Path: "music/assets/icon.png", Size: 900, Binary: true},
	}
}

// List mode strips every file body.
func TestStripSkillFileText(t *testing.T) {
	got := stripSkillFileText(sampleSkillFiles())
	if len(got) != 3 {
		t.Fatalf("want 3 entries, got %d", len(got))
	}
	for _, f := range got {
		if f.Text != "" || f.Truncated {
			t.Errorf("%s still carries a body: text=%q truncated=%v", f.Path, f.Text, f.Truncated)
		}
	}
	if got[0].Path != "music/SKILL.md" || got[0].Size != 12 {
		t.Errorf("metadata lost: %+v", got[0])
	}
	if !got[2].Binary {
		t.Error("binary flag lost")
	}
}

func TestCapSkillFileText(t *testing.T) {
	small := domain.SkillBundleFile{Path: "a.md", Size: 7, Text: "small"}
	if got := capSkillFileText(small); got != small {
		t.Errorf("small file was modified: %+v", got)
	}

	big := strings.Repeat("a", mqttMaxFileTextBytes+500)
	got := capSkillFileText(domain.SkillBundleFile{Path: "b.md", Size: int64(len(big)), Text: big})
	if !got.Truncated {
		t.Error("oversized file must be flagged truncated")
	}
	if got.Text != strings.Repeat("a", mqttMaxFileTextBytes) {
		t.Errorf("text = %q, want exactly %d bytes", got.Text, mqttMaxFileTextBytes)
	}
	if got.Size != int64(len(big)) {
		t.Errorf("size = %d, want %d", got.Size, len(big))
	}
}

func TestCapSkillFileTextKeepsValidUTF8(t *testing.T) {
	text := strings.Repeat("a", mqttMaxFileTextBytes-1) + "日本語"
	got := capSkillFileText(domain.SkillBundleFile{Path: "c.md", Text: text})

	if !utf8.ValidString(got.Text) {
		t.Errorf("text is not valid UTF-8: %q", got.Text)
	}
	if len(got.Text) > mqttMaxFileTextBytes {
		t.Errorf("text has %d bytes, cap is %d", len(got.Text), mqttMaxFileTextBytes)
	}
	if !got.Truncated {
		t.Error("must be flagged truncated")
	}
}
