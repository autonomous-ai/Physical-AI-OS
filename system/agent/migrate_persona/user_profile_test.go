package migratepersona

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// liveDeviceUserMD is the lamp-ac82 USER.md regression fixture (duplicate **Name:** bullets).
const liveDeviceUserMD = `- _Learn about the person you're helping. Update this as you go._
- **Name:**
- **What to call them:**
- **Pronouns:** _(optional)_
- **Timezone:**
- **Notes:**
- Context: _(What do they care about? What projects are they working on? What annoys them? What makes them laugh? Build this over time.)_
- Context: ---
- Context: The more you know, the better you can help. But remember — you're learning about a person, not building a dossier. Respect the difference.
- Related: [Agent workspace](/concepts/agent-workspace)
- **Name:** Leo
- Context: Leo speaks Vietnamese primarily (vi). He tends to respond well to music suggestions when in a low mood.
`

func writeTempUserMD(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "USER.md")
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatalf("seed USER.md: %v", err)
	}
	return p
}

func runUserProfileWrite(t *testing.T, dest string, incoming []string) string {
	t.Helper()
	m := &baseMigrator{opts: Options{Execute: true}.withDefaults()}
	m.writeUserProfile("user-profile", incoming, dest, DefaultUserCharLimit, openclawFormat)
	got, err := os.ReadFile(dest)
	if err != nil {
		t.Fatalf("read result: %v", err)
	}
	return string(got)
}

// A new name replaces the old one instead of being appended.
func TestWriteUserProfileReplacesNameInsteadOfAppending(t *testing.T) {
	dest := writeTempUserMD(t, liveDeviceUserMD)
	got := runUserProfileWrite(t, dest, []string{"**Name:** Long"})

	if !strings.Contains(got, "- **Name:** Long") {
		t.Errorf("new name not written:\n%s", got)
	}
	if n := strings.Count(got, "**Name:**"); n != 1 {
		t.Errorf("want exactly one Name bullet, got %d:\n%s", n, got)
	}
	if strings.Contains(got, "**Name:** Leo") {
		t.Errorf("retired name survived as a field:\n%s", got)
	}

	// Only the singular field is retired; free-form prose stays.
	if !strings.Contains(got, "Leo speaks Vietnamese") {
		t.Errorf("free-form entries must be left alone by field replacement:\n%s", got)
	}
}

// The USER.md template survives a migration verbatim; only filled fields change.
func TestWriteUserProfileKeepsTheEntireTemplate(t *testing.T) {
	dest := writeTempUserMD(t, liveDeviceUserMD)
	got := runUserProfileWrite(t, dest, []string{"**Name:** Long"})

	for _, want := range []string{
		"- _Learn about the person you're helping. Update this as you go._",
		"- **What to call them:**",
		"- **Pronouns:** _(optional)_",
		"- **Timezone:**",
		"- **Notes:**",
		"- Context: _(What do they care about?",
		"- Context: ---",
		"- Context: The more you know, the better you can help.",
		"- Related: [Agent workspace](/concepts/agent-workspace)",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("template line %q was lost:\n%s", want, got)
		}
	}
}

// The blank slot is filled in place and the appended retired value goes.
func TestWriteUserProfileFillsTheSlotInPlace(t *testing.T) {
	dest := writeTempUserMD(t, liveDeviceUserMD)
	got := runUserProfileWrite(t, dest, []string{"**Name:** Long"})

	lines := strings.Split(strings.TrimRight(got, "\n"), "\n")
	if len(lines) < 2 || lines[1] != "- **Name:** Long" {
		t.Errorf("Name must be filled in the slot it already occupies (line 2), got:\n%s", got)
	}
	if !strings.HasPrefix(lines[0], "- _Learn about the person") {
		t.Errorf("the instruction must stay above the fields:\n%s", got)
	}
}

// The template instruction is deduped to one copy across migrations.
func TestWriteUserProfileDoesNotAccumulateInstructions(t *testing.T) {
	dest := writeTempUserMD(t, liveDeviceUserMD)
	runUserProfileWrite(t, dest, splitLines(liveDeviceUserMD))
	got := runUserProfileWrite(t, dest, splitLines(liveDeviceUserMD))
	if n := strings.Count(got, "Update this as you go"); n != 1 {
		t.Errorf("want one copy of the instruction, got %d:\n%s", n, got)
	}
}

// splitLines turns a rendered USER.md back into writer entries.
func splitLines(doc string) []string {
	var out []string
	for _, l := range strings.Split(doc, "\n") {
		if e := strings.TrimPrefix(strings.TrimSpace(l), "- "); e != "" {
			out = append(out, e)
		}
	}
	return out
}

// An empty source profile never erases a learned name.
func TestWriteUserProfileNeverBlanksAFilledField(t *testing.T) {
	seeded := "- **Name:** Long\n- Context: Prefers Vietnamese.\n"

	for name, incoming := range map[string][]string{
		"no incoming entries at all": nil,
		"incoming has empty Name":    {"**Name:**"},
		"incoming Name is a hint":    {"**Name:** _(who are they?)_"},
		"incoming is unrelated":      {"Context: Likes lo-fi."},
	} {
		t.Run(name, func(t *testing.T) {
			dest := writeTempUserMD(t, seeded)
			got := runUserProfileWrite(t, dest, incoming)
			if !strings.Contains(got, "- **Name:** Long") {
				t.Errorf("existing name was lost:\n%s", got)
			}
		})
	}
}

func TestWriteUserProfileStillMergesFreeFormEntries(t *testing.T) {
	dest := writeTempUserMD(t, "- **Name:** Long\n- Context: Prefers Vietnamese.\n")
	got := runUserProfileWrite(t, dest, []string{"Context: Works late on Fridays."})

	for _, want := range []string{"Prefers Vietnamese.", "Works late on Fridays."} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q — free-form entries must merge, not replace:\n%s", want, got)
		}
	}
}

// A missing slot gets the field bullet appended.
func TestWriteUserProfileAppendsWhenThereIsNoSlot(t *testing.T) {
	dest := writeTempUserMD(t, "- Context: An earlier note.\n")
	got := runUserProfileWrite(t, dest, []string{"**Name:** Long"})

	if !strings.Contains(got, "- **Name:** Long") {
		t.Errorf("field not written when no slot exists:\n%s", got)
	}
	if !strings.Contains(got, "- Context: An earlier note.") {
		t.Errorf("existing content lost:\n%s", got)
	}
}

// An unfilled slot with no value stays as is.
func TestWriteUserProfileLeavesUnknownFieldSlotsBlank(t *testing.T) {
	dest := writeTempUserMD(t, "- **Name:**\n- **Timezone:**\n")
	got := runUserProfileWrite(t, dest, []string{"**Name:** Long"})

	if !strings.Contains(got, "- **Timezone:**\n") {
		t.Errorf("blank slot for an unknown field must be preserved:\n%s", got)
	}
	if !strings.Contains(got, "- **Name:** Long") {
		t.Errorf("known field not filled:\n%s", got)
	}
}

// A non-singular bold bullet is not dropped.
func TestWriteUserProfileKeepsNonProfileBoldBullets(t *testing.T) {
	dest := writeTempUserMD(t, "- **Notes:** Allergic to cilantro.\n")
	got := runUserProfileWrite(t, dest, []string{"**Name:** Long"})
	if !strings.Contains(got, "Allergic to cilantro") {
		t.Errorf("non-profile bold bullet was dropped:\n%s", got)
	}
}

func TestWriteUserProfileIsIdempotent(t *testing.T) {
	dest := writeTempUserMD(t, liveDeviceUserMD)
	first := runUserProfileWrite(t, dest, []string{"**Name:** Long"})
	second := runUserProfileWrite(t, dest, []string{"**Name:** Long"})
	if first != second {
		t.Errorf("not idempotent:\nfirst:\n%s\nsecond:\n%s", first, second)
	}
}

func TestWriteUserProfileDryRunTouchesNothing(t *testing.T) {
	dest := writeTempUserMD(t, liveDeviceUserMD)
	m := &baseMigrator{opts: Options{Execute: false}.withDefaults()}
	m.writeUserProfile("user-profile", []string{"**Name:** Long"}, dest, DefaultUserCharLimit, openclawFormat)

	got, err := os.ReadFile(dest)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	if string(got) != liveDeviceUserMD {
		t.Errorf("dry run modified the file:\n%s", got)
	}
}

func TestPartitionUserFieldsPrefersTheLaterFilledDuplicate(t *testing.T) {
	fields, _ := partitionUserFields([]string{"**Name:** Leo", "**Name:** Long"})
	if len(fields) != 1 || fields[0].value != "Long" {
		t.Fatalf("want the later value to win, got %+v", fields)
	}
}
