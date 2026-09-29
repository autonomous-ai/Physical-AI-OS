package jevskills

import (
	"context"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"unicode/utf8"

	"github.com/goccy/go-yaml"
)

type skill struct{ Name, Description, Path, Dir, Content string }

var skillName = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$`)

// readRegular refuses symlinks, pipes/devices, invalid UTF-8 and oversized files.
// Runtime adapters call this on local config and approved static skill roots.
func readRegular(path string, limit int64) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Size() > limit {
		return nil, errors.New("invalid file")
	}
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, errors.New("open file")
	}
	defer f.Close()
	current, err := f.Stat()
	if err != nil || !current.Mode().IsRegular() || !os.SameFile(info, current) {
		return nil, errors.New("file changed")
	}
	data, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil || int64(len(data)) > limit || !utf8.Valid(data) {
		return nil, errors.New("invalid file content")
	}
	return data, nil
}

// ReadPolicyFile reads native runtime policy without following a file symlink
// or accepting an unbounded document. Missing files retain os.IsNotExist.
func ReadPolicyFile(path string) ([]byte, error) { return readRegular(path, 1<<20) }

func (r *Router) catalog(ctx context.Context) ([]skill, error) {
	if r.opts.Allowed != nil && !r.opts.Allowed() {
		return nil, nil
	}
	roots := []string{r.opts.SkillsDir}
	if r.opts.AdditionalRoots != nil {
		roots = append(roots, r.opts.AdditionalRoots()...)
	}
	seenRoots, seenNames := map[string]bool{}, map[string]bool{}
	visited := 0
	var found []skill
	for _, dir := range roots {
		if dir == "" {
			return nil, errors.New("invalid skill root")
		}
		root, err := filepath.Abs(dir)
		if err != nil {
			return nil, err
		}
		if seenRoots[root] {
			continue
		}
		seenRoots[root] = true
		items, err := r.catalogRoot(ctx, root, seenNames, &visited)
		if err != nil {
			return nil, err
		}
		found = append(found, items...)
	}
	return found, nil
}

func (r *Router) catalogRoot(ctx context.Context, root string, seen map[string]bool, visited *int) ([]skill, error) {
	info, err := os.Lstat(root)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, errors.New("invalid skill root")
	}
	// Anchor file reads to a directory descriptor. A renamed parent or symlink
	// introduced while walking must not redirect a skill read outside this root.
	anchor, err := os.OpenRoot(root)
	if err != nil {
		return nil, errors.New("open skill root")
	}
	defer anchor.Close()
	anchoredInfo, err := anchor.Stat(".")
	if err != nil || !os.SameFile(info, anchoredInfo) {
		return nil, errors.New("skill root changed")
	}
	var found []skill
	err = filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if walkErr != nil {
			return errors.New("skill directory unavailable")
		}
		(*visited)++
		if *visited > 4096 {
			return errors.New("skill directory too large")
		}
		if path == root {
			return nil
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		if entry.IsDir() {
			if strings.HasPrefix(entry.Name(), ".") || strings.Count(rel, string(filepath.Separator)) > 4 {
				return filepath.SkipDir
			}
			return nil
		}
		if entry.Name() != "SKILL.md" || entry.Type()&os.ModeSymlink != 0 {
			return nil
		}
		f, err := anchor.OpenFile(rel, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
		if err != nil {
			return nil
		}
		fileInfo, err := f.Stat()
		if err != nil || !fileInfo.Mode().IsRegular() || fileInfo.Size() > maxSkill {
			f.Close()
			return nil
		}
		data, err := io.ReadAll(io.LimitReader(f, maxSkill+1))
		f.Close()
		if err != nil || len(data) > maxSkill || !utf8.Valid(data) {
			return nil
		}
		content := string(data)
		front, ok := frontmatter(content)
		if !ok {
			return nil
		}
		name, _ := front["name"].(string)
		description, _ := front["description"].(string)
		if !skillName.MatchString(name) {
			return nil
		}
		// Reserve the name even for unsupported metadata: a disabled/native-only
		// namesake must not expose a different copy from another skill root.
		if seen[name] {
			return errors.New("ambiguous skill name")
		}
		seen[name] = true
		if strings.TrimSpace(description) == "" || !simpleSkill(front, content) {
			return nil
		}
		for _, sidecar := range r.opts.NativeSidecars {
			if _, err := anchor.Lstat(filepath.Join(filepath.Dir(rel), sidecar)); !os.IsNotExist(err) {
				return nil
			}
		}
		if r.opts.Eligible != nil && !r.opts.Eligible(name, front) {
			return nil
		}
		found = append(found, skill{name, description, path, filepath.Dir(path), content})
		return nil
	})
	return found, err
}

func frontmatter(content string) (map[string]any, bool) {
	lines := strings.SplitN(strings.ReplaceAll(content, "\r\n", "\n"), "\n", 2)
	if len(lines) != 2 || lines[0] != "---" {
		return nil, false
	}
	end := strings.Index("\n"+lines[1], "\n---\n")
	if end < 0 || end > 16384 {
		return nil, false
	}
	var front map[string]any
	if yaml.Unmarshal([]byte(lines[1][:end]), &front) != nil || front == nil {
		return nil, false
	}
	return front, true
}

// A text preload cannot reproduce native subagent contexts, dynamic expansion,
// tool allowlists or dependency filters. Leave those skills to the native loader.
// Unknown frontmatter is deliberately excluded rather than silently bypassed.
func simpleSkill(front map[string]any, content string) bool {
	if strings.Contains(content, "!`") || strings.Contains(content, "$ARGUMENTS") || strings.Contains(content, "${") {
		return false
	}
	for key, value := range front {
		switch key {
		case "name", "description", "version", "license", "author", "homepage", "compatibility":
		case "disable-model-invocation":
			if value != false {
				return false
			}
		case "user-invocable":
			if value != true {
				return false
			}
		default:
			return false
		}
	}
	return true
}

// ProjectSkillDirs includes the current directory and its ancestors only when
// a repository boundary exists. Without one, parent trust is unknown.
func ProjectSkillDirs(workspace string, folders ...string) []string {
	cwd, err := filepath.Abs(workspace)
	if err != nil || workspace == "" {
		return nil
	}
	dirs := []string{cwd}
	for dir := cwd; ; dir = filepath.Dir(dir) {
		if _, err := os.Lstat(filepath.Join(dir, ".git")); err == nil {
			dirs = nil
			for p := cwd; ; p = filepath.Dir(p) {
				dirs = append(dirs, p)
				if p == dir {
					break
				}
			}
			break
		}
		if filepath.Dir(dir) == dir {
			break
		}
	}
	var roots []string
	for _, dir := range dirs {
		for _, folder := range folders {
			roots = append(roots, filepath.Join(dir, folder, "skills"))
		}
	}
	return roots
}
