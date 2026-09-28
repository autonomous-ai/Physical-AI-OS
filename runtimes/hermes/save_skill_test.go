package hermes

import "testing"

// The device-owned root must stay out of openclaw-imports: presync restores imports only when that dir is empty.
func TestAuthoredSkillsRootIsSeparateFromImports(t *testing.T) {
	if hermesAuthoredSkillsDir == hermesImportedSkillsDir {
		t.Fatal("authored skills must not share the migrate-owned imports root")
	}
	if want := hermesHome + "/skills/authored"; hermesAuthoredSkillsDir != want {
		t.Errorf("authored root = %q, want %q", hermesAuthoredSkillsDir, want)
	}
	if want := hermesHome + "/skills/openclaw-imports"; hermesImportedSkillsDir != want {
		t.Errorf("imports root = %q, want %q", hermesImportedSkillsDir, want)
	}
}
