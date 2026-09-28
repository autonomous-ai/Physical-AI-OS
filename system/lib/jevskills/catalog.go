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
// Runtime adapters only call this on local OS-managed config/skill directories.
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
	root, err := filepath.Abs(r.opts.SkillsDir)
	if err != nil || r.opts.SkillsDir == "" {
		return nil, errors.New("invalid skill root")
	}
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
	seen := map[string]bool{}
	visited := 0
	err = filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if walkErr != nil {
			return errors.New("skill directory unavailable")
		}
		visited++
		if visited > 4096 {
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
		if !ok || !simpleSkill(front, content) {
			return nil
		}
		name, _ := front["name"].(string)
		description, _ := front["description"].(string)
		if !skillName.MatchString(name) || strings.TrimSpace(description) == "" {
			return nil
		}
		if r.opts.Eligible != nil && !r.opts.Eligible(name, front) {
			return nil
		}
		if seen[name] {
			return errors.New("ambiguous skill name")
		}
		seen[name] = true
		found = append(found, skill{name, description, path, filepath.Dir(path), content})
		if len(found) > 32 {
			return errors.New("too many skills")
		}
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
