package http

import (
	"os"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/lib/i18n"
)

// PrewarmFillers enumerates pools from the pool maps, not a hand-kept list.
func TestPrewarmEnumeratesPoolsFromTheMaps(t *testing.T) {
	src, err := os.ReadFile("deadair_filler.go")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(src), "i18n.AllPoolKeys()") {
		t.Error("PrewarmFillers must enumerate pools via i18n.AllPoolKeys()")
	}
	for _, gone := range []string{`"web_search", "x_search"`, `"pdf", "canvas"`} {
		if strings.Contains(string(src), gone) {
			t.Errorf("a hand-maintained tool list is back in the prewarm: %s", gone)
		}
	}
}

// Every prewarmed phrase is reachable through the runtime lookup in every language.
func TestEveryPrewarmedPoolResolvesInEveryLanguage(t *testing.T) {
	keys := i18n.AllPoolKeys()
	if len(keys) == 0 {
		t.Fatal("no pool keys to prewarm")
	}
	for _, lang := range []string{i18n.LangEN, i18n.LangVI, i18n.LangZhCN, i18n.LangZhTW, i18n.LangJA} {
		for _, k := range keys {
			if len(i18n.FillerForTool(lang, k)) == 0 {
				t.Errorf("pool %q resolves to nothing in %q — it would be prewarmed as silence", k, lang)
			}
		}
	}
}
