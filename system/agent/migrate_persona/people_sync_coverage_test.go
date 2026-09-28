package migratepersona

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Every runtime must instruct its agent to maintain USER.md `## Users` in the reconciler's format.
func TestEveryRuntimeTeachesThePeopleSync(t *testing.T) {
	root := filepath.Join("..", "..", "..", "runtimes")
	ents, err := os.ReadDir(root)
	if err != nil {
		t.Fatalf("read runtimes dir: %v", err)
	}

	var checked int
	for _, e := range ents {
		if !e.IsDir() {
			continue
		}
		src := filepath.Join(root, e.Name(), "onboarding.go")
		body, err := os.ReadFile(src)
		if err != nil {
			continue // not every dir is a runtime with an onboarding block
		}
		checked++
		text := string(body)

		if !strings.Contains(text, "label> (friend)") {
			t.Errorf("%s: no people-sync instruction — this runtime would stop maintaining USER.md's ## Users section", e.Name())
			continue
		}
		for _, want := range []string{
			"Only write what you observed about THAT person",
			"never carry a former user's traits",
			"never guess pronouns",
			"comes FIRST and only when they have TOLD you",
			"NOT flowing prose",
			"400 characters",
			"remove any you find",
			"Never delete a PERSON's entry",
		} {
			if !strings.Contains(text, want) {
				t.Errorf("%s: people sync is missing constraint %q", e.Name(), want)
			}
		}
	}
	if checked < 6 {
		t.Fatalf("only inspected %d runtimes; expected at least 6", checked)
	}
}
