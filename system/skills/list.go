package skills

import (
	"fmt"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// listMaxDepth bounds the tree walk (guards symlink loops).
const listMaxDepth = 6

// listMaxEntriesPerDir caps how many children one directory contributes.
const listMaxEntriesPerDir = 200

// ListInstalled returns skill dirs under skillsDir with file trees, sorted by name.
// A missing dir yields an empty list; .new/.old and dot-dirs are skipped.
func ListInstalled(skillsDir string) ([]domain.InstalledSkill, error) {
	if skillsDir == "" {
		return nil, fmt.Errorf("skills dir is not configured")
	}

	entries, err := os.ReadDir(skillsDir)
	if err != nil {
		if os.IsNotExist(err) {
			return []domain.InstalledSkill{}, nil
		}
		return nil, fmt.Errorf("read skills dir: %w", err)
	}

	out := make([]domain.InstalledSkill, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		name := e.Name()
		if strings.HasPrefix(name, ".") ||
			strings.HasSuffix(name, ".new") || strings.HasSuffix(name, ".old") {
			continue
		}

		dir := filepath.Join(skillsDir, name)
		files, newest, err := walkSkillTree(dir, name, 0)
		if err != nil {
			// One unreadable skill must not blank the whole list.
			files, newest = nil, time.Time{}
		}
		if newest.IsZero() {
			if info, err := e.Info(); err == nil {
				newest = info.ModTime()
			}
		}

		skill := domain.InstalledSkill{
			Name:        name,
			Description: readSkillDescription(filepath.Join(dir, "SKILL.md")),
			Files:       files,
		}
		if !newest.IsZero() {
			skill.UpdatedAt = newest.Unix()
		}
		out = append(out, skill)
	}

	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

// ListInstalledFrom merges several skill roots; the first occurrence of a name wins.
func ListInstalledFrom(dirs ...string) ([]domain.InstalledSkill, error) {
	seen := make(map[string]bool)
	var out []domain.InstalledSkill

	for _, dir := range dirs {
		list, err := ListInstalled(dir)
		if err != nil {
			return nil, err
		}
		for _, s := range list {
			if seen[s.Name] {
				continue
			}
			seen[s.Name] = true
			out = append(out, s)
		}
	}

	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	if out == nil {
		out = []domain.InstalledSkill{}
	}
	return out, nil
}

// walkSkillTree returns dir's nodes (dirs first, alphabetical) and the newest mtime.
// relBase prefixes each node path, e.g. "music/SKILL.md".
func walkSkillTree(dir, relBase string, depth int) ([]domain.SkillNode, time.Time, error) {
	if depth >= listMaxDepth {
		return nil, time.Time{}, nil
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, time.Time{}, err
	}
	if len(entries) > listMaxEntriesPerDir {
		entries = entries[:listMaxEntriesPerDir]
	}

	var newest time.Time
	bump := func(t time.Time) {
		if t.After(newest) {
			newest = t
		}
	}

	nodes := make([]domain.SkillNode, 0, len(entries))
	for _, e := range entries {
		name := e.Name()
		if strings.HasPrefix(name, ".") {
			continue
		}
		rel := path.Join(relBase, name)

		if e.IsDir() {
			children, childNewest, err := walkSkillTree(filepath.Join(dir, name), rel, depth+1)
			if err != nil {
				continue
			}
			bump(childNewest)
			nodes = append(nodes, domain.SkillNode{
				Name: name, Path: rel, Dir: true, Children: children,
			})
			continue
		}

		node := domain.SkillNode{Name: name, Path: rel}
		if info, err := e.Info(); err == nil {
			node.Size = info.Size()
			bump(info.ModTime())
		}
		nodes = append(nodes, node)
	}

	sort.Slice(nodes, func(i, j int) bool {
		if nodes[i].Dir != nodes[j].Dir {
			return nodes[i].Dir // dirs first
		}
		return nodes[i].Name < nodes[j].Name
	})
	return nodes, newest, nil
}

// readSkillDescription returns the SKILL.md description, or "" on any problem.
func readSkillDescription(skillMD string) string {
	content, err := os.ReadFile(skillMD)
	if err != nil {
		return ""
	}
	_, description, _ := scanFrontMatter(content)
	return description
}
