package migratepersona

import (
	"path/filepath"
	"sort"
)

// PersonaPaths returns every persona/memory path across ALL runtimes, deduped and sorted.
// All of them: a switch leaves copies behind, and an unwiped copy migrates back on the next switch.
func PersonaPaths(opts Options) []string {
	seen := map[string]bool{}
	var out []string
	for _, a := range adapters {
		for _, p := range a.personaPaths(opts) {
			if p == "" || seen[p] {
				continue
			}
			seen[p] = true
			out = append(out, p)
		}
	}
	sort.Strings(out)
	return out
}

// openclawLayoutPersonaPaths lists persona files (never the workspace dir) of the OpenClaw
// workspace layout, reused by codex/claudecode/opencode; picoclaw builds its own.
func openclawLayoutPersonaPaths(ws string) []string {
	if ws == "" {
		return nil
	}
	return []string{
		filepath.Join(ws, "SOUL.md"),
		filepath.Join(ws, "IDENTITY.md"),
		filepath.Join(ws, "MEMORY.md"),
		filepath.Join(ws, "USER.md"),
		filepath.Join(ws, "KNOWLEDGE.md"),
		filepath.Join(ws, "memory"),
	}
}
