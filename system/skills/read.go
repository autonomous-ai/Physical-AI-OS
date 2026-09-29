package skills

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"sort"
	"strings"
	"unicode/utf8"

	"go.autonomous.ai/os/system/domain"
)

const (
	// readMaxInlineBytes caps inlined text per file; longer files are marked Truncated.
	readMaxInlineBytes = 512 << 10
	// readMaxFileBytes caps how much of one file is read off disk at all.
	readMaxFileBytes = 2 << 20
	readMaxFiles     = 500
	readMaxDepth     = 6
	// binarySniffSize is how many leading bytes decide text-vs-binary.
	binarySniffSize = 8000
)

// ErrSkillFileNotFound is returned when an entry path is not a readable file in a skill.
var ErrSkillFileNotFound = errors.New("skill file not found")

// ReadSkillFiles returns every file in <skillsDir>/<name> as a flat, path-sorted list.
func ReadSkillFiles(skillsDir, name string) ([]domain.SkillBundleFile, error) {
	if err := ValidateSkillName(name); err != nil {
		return nil, err
	}
	if skillsDir == "" {
		return nil, fmt.Errorf("skills dir is not configured")
	}

	root := filepath.Join(skillsDir, name)
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		return nil, fmt.Errorf("skill %q not found", name)
	}

	var out []domain.SkillBundleFile
	if err := collectSkillFiles(root, name, 0, &out); err != nil {
		return nil, err
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Path < out[j].Path })
	return out, nil
}

// ReadSkillFilesFrom returns the skill from the first root that has it.
func ReadSkillFilesFrom(name string, dirs ...string) ([]domain.SkillBundleFile, error) {
	var lastErr error
	for _, dir := range dirs {
		files, err := ReadSkillFiles(dir, name)
		if err == nil {
			return files, nil
		}
		lastErr = err
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("skill %q not found", name)
	}
	return nil, lastErr
}

// ReadSkillFile reads one file; filePath is the relative path from ReadSkillFiles (incl. skill name).
func ReadSkillFile(skillsDir, name, filePath string) (domain.SkillBundleFile, error) {
	if err := ValidateSkillName(name); err != nil {
		return domain.SkillBundleFile{}, err
	}
	if skillsDir == "" {
		return domain.SkillBundleFile{}, fmt.Errorf("skills dir is not configured")
	}

	rel, err := skillFileRelativePath(name, filePath)
	if err != nil {
		return domain.SkillBundleFile{}, err
	}
	root := filepath.Join(skillsDir, name)
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		return domain.SkillBundleFile{}, fmt.Errorf("%w: skill %q", ErrSkillNotFound, name)
	}

	full := filepath.Join(root, filepath.FromSlash(rel))
	info, err = os.Stat(full)
	if err != nil || info.IsDir() {
		return domain.SkillBundleFile{}, fmt.Errorf("%w: %s", ErrSkillFileNotFound, filePath)
	}
	content, err := readCapped(full)
	if err != nil {
		return domain.SkillBundleFile{}, fmt.Errorf("read %s: %w", filePath, err)
	}
	return BuildFilePreview(filePath, content, info.Size()), nil
}

// ReadSkillFileFrom uses ReadSkillFilesFrom's root precedence; the first root with the skill wins.
func ReadSkillFileFrom(name, filePath string, dirs ...string) (domain.SkillBundleFile, error) {
	var lastErr error
	for _, dir := range dirs {
		file, err := ReadSkillFile(dir, name, filePath)
		if err == nil {
			return file, nil
		}
		if !errors.Is(err, ErrSkillNotFound) {
			return domain.SkillBundleFile{}, err
		}
		lastErr = err
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("%w: %s", ErrSkillNotFound, name)
	}
	return domain.SkillBundleFile{}, lastErr
}

func skillFileRelativePath(name, filePath string) (string, error) {
	prefix := name + "/"
	if !strings.HasPrefix(filePath, prefix) || path.Clean(filePath) != filePath {
		return "", fmt.Errorf("%w: %s", ErrSkillFileNotFound, filePath)
	}
	rel := strings.TrimPrefix(filePath, prefix)
	if rel == "" {
		return "", fmt.Errorf("%w: %s", ErrSkillFileNotFound, filePath)
	}
	parts := strings.Split(rel, "/")
	if len(parts) > readMaxDepth || strings.HasPrefix(parts[len(parts)-1], ".") {
		return "", fmt.Errorf("%w: %s", ErrSkillFileNotFound, filePath)
	}
	for _, part := range parts {
		if part == "" || strings.HasPrefix(part, ".") {
			return "", fmt.Errorf("%w: %s", ErrSkillFileNotFound, filePath)
		}
	}
	return rel, nil
}

// collectSkillFiles appends each file under dir; relBase prefixes paths.
func collectSkillFiles(dir, relBase string, depth int, out *[]domain.SkillBundleFile) error {
	if depth >= readMaxDepth || len(*out) >= readMaxFiles {
		return nil
	}

	entries, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("read %s: %w", dir, err)
	}

	for _, e := range entries {
		if len(*out) >= readMaxFiles {
			return nil
		}
		name := e.Name()
		if strings.HasPrefix(name, ".") {
			continue
		}
		rel := path.Join(relBase, name)

		if e.IsDir() {
			_ = collectSkillFiles(filepath.Join(dir, name), rel, depth+1, out)
			continue
		}

		full := filepath.Join(dir, name)
		var size int64
		if info, err := e.Info(); err == nil {
			size = info.Size()
		}
		content, err := readCapped(full)
		if err != nil {
			continue
		}
		*out = append(*out, BuildFilePreview(rel, content, size))
	}
	return nil
}

// readCapped reads at most readMaxFileBytes from path.
func readCapped(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return io.ReadAll(io.LimitReader(f, readMaxFileBytes))
}

// BuildFilePreview inlines valid UTF-8 without NULs as text, else reports binary metadata.
func BuildFilePreview(filePath string, content []byte, declaredSize int64) domain.SkillBundleFile {
	size := declaredSize
	if size <= 0 {
		size = int64(len(content))
	}
	file := domain.SkillBundleFile{Path: filePath, Size: size}

	sniff := content
	if len(sniff) > binarySniffSize {
		sniff = sniff[:binarySniffSize]
	}
	if !utf8.Valid(sniff) || strings.IndexByte(string(sniff), 0) >= 0 {
		file.Binary = true
		return file
	}

	if len(content) > readMaxInlineBytes {
		content = content[:readMaxInlineBytes]
		file.Truncated = true
		// Don't cut a multi-byte rune in half.
		for len(content) > 0 && !utf8.Valid(content) {
			content = content[:len(content)-1]
		}
	}
	file.Text = string(content)
	return file
}
