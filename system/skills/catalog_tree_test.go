package skills

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"testing"
)

// skillsDir is the tree this catalog is generated from: repo-root skills/.
const skillsDir = "../../skills"

type sidecar struct {
	Name         string   `json:"name"`
	Capabilities []string `json:"capabilities"`
}

// TestCatalogMatchesTree fails when catalog_gen.go is stale (run `make skills-catalog`).
func TestCatalogMatchesTree(t *testing.T) {
	entries, err := os.ReadDir(skillsDir)
	if err != nil {
		t.Skipf("skills/ not reachable from the test binary: %v", err)
	}

	onDisk := []string{}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		if _, err := os.Stat(filepath.Join(skillsDir, e.Name(), "SKILL.md")); err != nil {
			continue // a folder with no SKILL.md is not a skill
		}
		onDisk = append(onDisk, e.Name())
	}
	if len(onDisk) == 0 {
		t.Fatal("no skill folders found under skills/")
	}

	inCatalog := map[string]bool{}
	for _, n := range Catalog {
		inCatalog[n] = true
	}
	for _, n := range onDisk {
		if !inCatalog[n] {
			t.Errorf("skills/%s/ exists but is not in Catalog — run `make skills-catalog`", n)
		}
	}
	byDisk := map[string]bool{}
	for _, n := range onDisk {
		byDisk[n] = true
	}
	for _, n := range Catalog {
		if !byDisk[n] {
			t.Errorf("Catalog lists %q but skills/%s/ has no SKILL.md — run `make skills-catalog`", n, n)
		}
	}
}

// TestCapabilityMatchesSidecars checks the generated map against every skill.json.
func TestCapabilityMatchesSidecars(t *testing.T) {
	entries, err := os.ReadDir(skillsDir)
	if err != nil {
		t.Skipf("skills/ not reachable from the test binary: %v", err)
	}

	want := map[string][]string{}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(skillsDir, e.Name(), "skill.json"))
		if err != nil {
			continue // no sidecar → platform skill, no hardware requirement
		}
		var sc sidecar
		if err := json.Unmarshal(raw, &sc); err != nil {
			t.Errorf("skills/%s/skill.json is not valid JSON: %v", e.Name(), err)
			continue
		}
		if len(sc.Capabilities) > 0 {
			want[e.Name()] = sc.Capabilities
		}
	}

	if len(want) != len(Capability) {
		t.Errorf("sidecars declare %d skills with capabilities, Capability has %d — run `make skills-catalog`",
			len(want), len(Capability))
	}
	for name, caps := range want {
		got, ok := Capability[name]
		if !ok {
			t.Errorf("skills/%s/skill.json declares %v but Capability has no entry — run `make skills-catalog`", name, caps)
			continue
		}
		gotCopy := append([]string(nil), got...)
		wantCopy := append([]string(nil), caps...)
		sort.Strings(gotCopy)
		sort.Strings(wantCopy)
		if len(gotCopy) != len(wantCopy) {
			t.Errorf("%s: sidecar %v, catalog %v", name, caps, got)
			continue
		}
		for i := range gotCopy {
			if gotCopy[i] != wantCopy[i] {
				t.Errorf("%s: sidecar %v, catalog %v", name, caps, got)
				break
			}
		}
	}
}
