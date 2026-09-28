package hermes

import (
	"strings"
	"testing"
)

func TestSoulNameIgnoresManagedInstructions(t *testing.T) {
	// The real injected block that triggered the bogus wake words. It now lives in
	// AGENTS.md, but it carries a `**Name:**` line of its own, so the SOUL.md name
	// parser must stay immune to it wherever it turns up.
	if got := parseSoulName(agentsMDBlock); got != "" {
		t.Fatalf("instructions parsed as name: %q", got)
	}
	for _, card := range []string{"", "\n## Your identity card\n\n- **Name:** Noah\n"} {
		input := agentsMDBlock + card
		if card != "" && parseSoulName(input) != "Noah" {
			t.Fatal("instructions shadowed real identity card")
		}
		updated := rewriteSoulName(input, "Ngân")
		if !strings.HasPrefix(updated, agentsMDBlock) {
			t.Fatal("rename modified managed instructions")
		}
		if got := parseSoulName(updated); got != "Ngân" {
			t.Fatalf("renamed identity = %q", got)
		}
	}
}

func TestRewriteSoulName_ReplacesExisting(t *testing.T) {
	in := "# Soul\n\nYou are Lamp.\n\n## Your identity card\n\n- **Name:** Lamp\n"
	got := rewriteSoulName(in, "Ngân")
	want := "# Soul\n\nYou are Lamp.\n\n## Your identity card\n\n- **Name:** Ngân\n"
	if got != want {
		t.Fatalf("rewrite mismatch:\n got=%q\nwant=%q", got, want)
	}
}

func TestRewriteSoulName_AppendsWhenAbsent(t *testing.T) {
	in := "# Soul\n\nYou are Lamp.\n"
	got := rewriteSoulName(in, "Ngân")
	want := "# Soul\n\nYou are Lamp.\n\n## Your identity card\n\n- **Name:** Ngân\n"
	if got != want {
		t.Fatalf("append mismatch:\n got=%q\nwant=%q", got, want)
	}
}

func TestRewriteSoulName_PreservesBulletPrefix(t *testing.T) {
	// A non-bullet name line keeps whatever prefix precedes **Name:**.
	in := "**Name:** Old Description that should be dropped\n"
	got := rewriteSoulName(in, "Ngân")
	want := "**Name:** Ngân\n"
	if got != want {
		t.Fatalf("prefix mismatch:\n got=%q\nwant=%q", got, want)
	}
}

func TestParseSoulName(t *testing.T) {
	cases := []struct {
		name string
		soul string
		want string
	}{
		{"card line", "# Soul\n\nYou are Lamp.\n\n## Your identity card\n\n- **Name:** Ngân\n", "Ngân"},
		{"strips trailing description", "- **Name:** Noah — the desk companion\n", "Noah"},
		{"strips dash description", "- **Name:** Lamp - a living being\n", "Lamp"},
		{"no name line → empty", "# Soul\n\nYou are **Lamp**.\n", ""},
		{"empty value → empty", "- **Name:**\n", ""},
		{"star bullet and case", "  * **NAME:** Noah\n", "Noah"},
		{"inline example", "Set **Name:** Noah\n", ""},
		{"quoted example", "`**Name:**` or the other single-value fields\n", ""},
		{"first name line wins", "- **Name:** Ngân\n- **Name:** Hà\n", "Ngân"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := parseSoulName(c.soul); got != c.want {
				t.Fatalf("parseSoulName(%q) = %q, want %q", c.soul, got, c.want)
			}
		})
	}
}
