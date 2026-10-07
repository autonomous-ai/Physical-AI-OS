package hermes

import (
	"strings"
	"testing"
)

// A title heard in a voice turn is not an address preference: on 2026-10-05
// realtime heard "...is Lee" as "Miss Lee" and Hermes saved "call: Lee (Ms Lee)".
func TestPeopleRulesForbidHeardTitles(t *testing.T) {
	for _, want := range []string{
		"A title or honorific heard in a voice turn",
		"never infer gender",
		`("…is Lee" heard as "Miss Lee")`,
	} {
		if !strings.Contains(agentsMDBlock, want) {
			t.Fatalf("agentsMDBlock missing %q", want)
		}
	}
}
