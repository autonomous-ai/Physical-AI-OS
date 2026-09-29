package skills

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// ErrInvalidSkillName is returned when a skill name is empty or not a safe slug.
var ErrInvalidSkillName = errors.New("invalid skill name")

// ErrSkillExists is returned when authoring would overwrite an existing skill.
var ErrSkillExists = errors.New("skill already exists")

// skillNamePattern allows lowercase letters, digits, dash and underscore.
var skillNamePattern = regexp.MustCompile(`^[a-z0-9_-]+$`)

// maxSkillNameLen bounds the skill directory name.
const maxSkillNameLen = 64

// SkillMarkdownFile is the entry-point filename every skill must carry.
const SkillMarkdownFile = "SKILL.md"

// ErrMissingSkillMD is returned when an archive has no SKILL.md at the skill root.
var ErrMissingSkillMD = errors.New("archive has no " + SkillMarkdownFile + " at the skill root")

// ErrInvalidFrontMatter is returned when SKILL.md front-matter lacks name/description.
var ErrInvalidFrontMatter = errors.New("invalid SKILL.md front-matter")

// ParseSkillFrontMatter reads top-level `name` and `description` from SKILL.md front-matter.
// Extra keys are ignored; returns ErrInvalidFrontMatter when either is missing.
func ParseSkillFrontMatter(content []byte) (name, description string, err error) {
	name, description, ok := scanFrontMatter(content)
	if !ok || name == "" || description == "" {
		return "", "", ErrInvalidFrontMatter
	}
	return name, description, nil
}

// scanFrontMatter leniently returns whatever keys it found and whether a block exists.
func scanFrontMatter(content []byte) (name, description string, ok bool) {
	sc := bufio.NewScanner(bytes.NewReader(content))
	sc.Buffer(make([]byte, 0, 64*1024), 1<<20)

	opened := false
	for sc.Scan() {
		raw := strings.TrimRight(sc.Text(), "\r")
		line := strings.TrimSpace(raw)

		if line == "---" {
			if !opened {
				opened = true
				continue
			}
			break // closing delimiter — whatever we collected is it
		}
		if !opened {
			if line == "" {
				continue
			}
			return "", "", false
		}

		// Only top-level keys count (ignore `name:` nested under `metadata:`).
		if raw != line {
			continue
		}

		if rest, ok := strings.CutPrefix(line, "name:"); ok {
			name = strings.Trim(strings.TrimSpace(rest), `"'`)
			continue
		}
		if rest, ok := strings.CutPrefix(line, "description:"); ok {
			description = strings.Trim(strings.TrimSpace(rest), `"'`)
		}
	}

	return name, description, opened
}

// InstallSkillMarkdown installs a bare SKILL.md under its front-matter name,
// replacing an existing skill of that name.
func InstallSkillMarkdown(skillsDir string, content []byte) (string, error) {
	if skillsDir == "" {
		return "", errors.New("skills dir is not configured")
	}

	name, _, err := ParseSkillFrontMatter(content)
	if err != nil {
		return "", err
	}
	if err := ValidateSkillName(name); err != nil {
		return "", err
	}

	target := filepath.Join(skillsDir, name)
	staging := target + ".new"
	if err := os.RemoveAll(staging); err != nil {
		return "", fmt.Errorf("clean staging dir: %w", err)
	}
	if err := os.MkdirAll(staging, 0755); err != nil {
		return "", fmt.Errorf("create staging dir: %w", err)
	}
	if err := os.WriteFile(filepath.Join(staging, SkillMarkdownFile), content, 0644); err != nil {
		_ = os.RemoveAll(staging)
		return "", fmt.Errorf("write %s: %w", SkillMarkdownFile, err)
	}

	// Atomic swap: the previous version is restored if the rename fails.
	backup := target + ".old"
	_ = os.RemoveAll(backup)
	if _, err := os.Stat(target); err == nil {
		if err := os.Rename(target, backup); err != nil {
			_ = os.RemoveAll(staging)
			return "", fmt.Errorf("replace existing skill %s: %w", name, err)
		}
	}
	if err := os.Rename(staging, target); err != nil {
		_ = os.Rename(backup, target)
		_ = os.RemoveAll(staging)
		return "", fmt.Errorf("install skill %s: %w", name, err)
	}
	_ = os.RemoveAll(backup)

	return target, nil
}

// SlugifySkillName coerces a label into a skill slug; "" when nothing usable survives.
// Example: "My Report.v2" -> "my-report-v2"
func SlugifySkillName(label string) string {
	var b strings.Builder
	prevDash := false
	for _, r := range strings.ToLower(strings.TrimSpace(label)) {
		switch {
		case (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '_' || r == '-':
			b.WriteRune(r)
			prevDash = r == '-'
		default:
			if !prevDash && b.Len() > 0 {
				b.WriteByte('-')
				prevDash = true
			}
		}
	}

	out := strings.Trim(b.String(), "-_")
	if len(out) > maxSkillNameLen {
		out = strings.Trim(out[:maxSkillNameLen], "-_")
	}
	return out
}

// ValidateSkillName reports whether name is usable as a skill directory.
func ValidateSkillName(name string) error {
	if len(name) > maxSkillNameLen {
		return fmt.Errorf("%w: longer than %d characters", ErrInvalidSkillName, maxSkillNameLen)
	}
	if !skillNamePattern.MatchString(name) {
		return fmt.Errorf("%w: %q (lowercase letters, digits, _ and - only)", ErrInvalidSkillName, name)
	}
	return nil
}

// RenderSkillMarkdown builds SKILL.md with name/description front-matter and the body.
// The description is flattened to one line so it cannot break the YAML block.
func RenderSkillMarkdown(name, description, instructions string) string {
	desc := strings.Join(strings.Fields(description), " ")

	var b strings.Builder
	b.WriteString("---\n")
	fmt.Fprintf(&b, "name: %s\n", name)
	fmt.Fprintf(&b, "description: %s\n", desc)
	b.WriteString("---\n\n")
	b.WriteString(strings.TrimRight(instructions, "\n"))
	b.WriteString("\n")
	return b.String()
}

// ErrSkillNotFound is returned when a skill directory isn't there to remove.
var ErrSkillNotFound = errors.New("skill not found")

// DeleteSkill removes <skillsDir>/<name> after validating the name; missing -> ErrSkillNotFound.
func DeleteSkill(skillsDir, name string) (string, error) {
	if err := ValidateSkillName(name); err != nil {
		return "", err
	}
	if skillsDir == "" {
		return "", errors.New("skills dir is not configured")
	}

	dir := filepath.Join(skillsDir, name)
	info, err := os.Stat(dir)
	if os.IsNotExist(err) {
		return "", fmt.Errorf("%w: %s", ErrSkillNotFound, name)
	}
	if err != nil {
		return "", fmt.Errorf("stat %s: %w", dir, err)
	}
	if !info.IsDir() {
		// Refuse to delete a path that is not a skill directory.
		return "", fmt.Errorf("%w: %s is not a skill directory", ErrSkillNotFound, name)
	}

	if err := os.RemoveAll(dir); err != nil {
		return "", fmt.Errorf("remove %s: %w", dir, err)
	}
	return dir, nil
}

// DeleteSkillFrom removes a skill from the first root that has it (device root first).
func DeleteSkillFrom(name string, dirs ...string) (string, error) {
	var lastErr error
	for _, dir := range dirs {
		path, err := DeleteSkill(dir, name)
		if err == nil {
			return path, nil
		}
		// Only keep looking when this root simply lacked the skill.
		if !errors.Is(err, ErrSkillNotFound) {
			return "", err
		}
		lastErr = err
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("%w: %s", ErrSkillNotFound, name)
	}
	return "", lastErr
}

// WriteAuthoredSkill creates <skillsDir>/<name>/SKILL.md; never clobbers (ErrSkillExists).
func WriteAuthoredSkill(skillsDir, name, description, instructions string) (string, error) {
	if err := ValidateSkillName(name); err != nil {
		return "", err
	}
	if strings.TrimSpace(description) == "" {
		return "", errors.New("description is required")
	}
	if strings.TrimSpace(instructions) == "" {
		return "", errors.New("instructions are required")
	}
	if skillsDir == "" {
		return "", errors.New("skills dir is not configured")
	}

	dir := filepath.Join(skillsDir, name)
	if _, err := os.Stat(dir); err == nil {
		return "", fmt.Errorf("%w: %s", ErrSkillExists, name)
	} else if !os.IsNotExist(err) {
		return "", fmt.Errorf("stat %s: %w", dir, err)
	}

	if err := os.MkdirAll(dir, 0755); err != nil {
		return "", fmt.Errorf("create skill dir %s: %w", dir, err)
	}

	path := filepath.Join(dir, "SKILL.md")
	content := RenderSkillMarkdown(name, description, instructions)
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		_ = os.RemoveAll(dir)
		return "", fmt.Errorf("write %s: %w", path, err)
	}
	return path, nil
}
