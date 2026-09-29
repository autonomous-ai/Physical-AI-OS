package skills

import (
	"archive/zip"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// ErrEmptyArchive is returned when the archive holds no usable files.
var ErrEmptyArchive = errors.New("skill archive is empty")

// installMaxFiles / installMaxFileBytes bound what one archive can write.
const (
	installMaxFiles     = 500
	installMaxFileBytes = 4 << 20
)

// InstallSkillArchive extracts archivePath into skillsDir as one skill; returns path and file count.
// Staged under <name>.new and swapped in atomically; an existing skill is replaced.
func InstallSkillArchive(archivePath, skillsDir, fallbackName string) (string, int, error) {
	if skillsDir == "" {
		return "", 0, errors.New("skills dir is not configured")
	}

	r, err := zip.OpenReader(archivePath)
	if err != nil {
		return "", 0, fmt.Errorf("open skill archive: %w", err)
	}
	defer r.Close()

	entries := usableEntries(r)
	if len(entries) == 0 {
		return "", 0, ErrEmptyArchive
	}
	if len(entries) > installMaxFiles {
		return "", 0, fmt.Errorf("skill archive has %d files (max %d)", len(entries), installMaxFiles)
	}

	// A single shared top-level dir names the skill; otherwise fallbackName does.
	name, strip := archiveSkillName(entries)
	if name == "" {
		name = strings.TrimSpace(fallbackName)
	}
	if err := ValidateSkillName(name); err != nil {
		return "", 0, err
	}

	target := filepath.Join(skillsDir, name)
	staging := target + ".new"
	if err := os.RemoveAll(staging); err != nil {
		return "", 0, fmt.Errorf("clean staging dir: %w", err)
	}
	if err := os.MkdirAll(staging, 0755); err != nil {
		return "", 0, fmt.Errorf("create staging dir: %w", err)
	}

	count, err := extractEntries(entries, strip, staging)
	if err != nil {
		_ = os.RemoveAll(staging)
		return "", 0, err
	}
	if count == 0 {
		_ = os.RemoveAll(staging)
		return "", 0, ErrEmptyArchive
	}

	// Checked on the staging copy so a rejected archive never reaches the live tree.
	if _, err := os.Stat(filepath.Join(staging, SkillMarkdownFile)); err != nil {
		_ = os.RemoveAll(staging)
		return "", 0, ErrMissingSkillMD
	}

	// Old dir is moved aside first so a failed rename can be undone.
	backup := target + ".old"
	_ = os.RemoveAll(backup)
	if _, err := os.Stat(target); err == nil {
		if err := os.Rename(target, backup); err != nil {
			_ = os.RemoveAll(staging)
			return "", 0, fmt.Errorf("replace existing skill %s: %w", name, err)
		}
	}
	if err := os.Rename(staging, target); err != nil {
		_ = os.Rename(backup, target) // put the working skill back
		_ = os.RemoveAll(staging)
		return "", 0, fmt.Errorf("install skill %s: %w", name, err)
	}
	_ = os.RemoveAll(backup)

	return target, count, nil
}

// usableEntries drops directories and OS cruft; path safety is checked at extract.
func usableEntries(r *zip.ReadCloser) []*zip.File {
	out := make([]*zip.File, 0, len(r.File))
	for _, f := range r.File {
		if f.FileInfo().IsDir() {
			continue
		}
		name := filepath.ToSlash(f.Name)
		if strings.HasPrefix(name, "__MACOSX/") || filepath.Base(name) == ".DS_Store" {
			continue
		}
		out = append(out, f)
	}
	return out
}

// archiveSkillName returns the single common top-level dir (strip=true), else ("", false).
func archiveSkillName(entries []*zip.File) (string, bool) {
	top := ""
	for _, f := range entries {
		name := filepath.ToSlash(f.Name)
		i := strings.Index(name, "/")
		if i <= 0 {
			return "", false // a file at the root — no single wrapping dir
		}
		seg := name[:i]
		if top == "" {
			top = seg
			continue
		}
		if seg != top {
			return "", false
		}
	}
	return top, top != ""
}

// extractEntries writes entries into dest with a traversal guard and forced 0644/0755 perms.
func extractEntries(entries []*zip.File, strip bool, dest string) (int, error) {
	cleanDest, err := filepath.Abs(dest)
	if err != nil {
		return 0, fmt.Errorf("resolve dest: %w", err)
	}
	cleanDest = filepath.Clean(cleanDest) + string(os.PathSeparator)

	count := 0
	for _, f := range entries {
		rel := filepath.ToSlash(f.Name)
		if strip {
			if i := strings.Index(rel, "/"); i >= 0 {
				rel = rel[i+1:]
			}
		}
		if rel == "" {
			continue
		}
		if strings.HasPrefix(rel, "/") || strings.Contains(rel, "..") {
			return count, fmt.Errorf("unsafe zip entry %q", f.Name)
		}

		target := filepath.Join(dest, filepath.FromSlash(rel))
		absTarget, err := filepath.Abs(target)
		if err != nil || !strings.HasPrefix(absTarget+string(os.PathSeparator), cleanDest) {
			return count, fmt.Errorf("zip entry escapes dest: %q", f.Name)
		}
		if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
			return count, fmt.Errorf("mkdir %s: %w", filepath.Dir(target), err)
		}
		if err := writeZipFile(f, target); err != nil {
			return count, err
		}
		count++
	}
	return count, nil
}

// copyLimited copies at most limit bytes, erroring if the source has more (zip bomb guard).
func copyLimited(dst io.Writer, src io.Reader, limit int64) (int64, error) {
	n, err := io.Copy(dst, io.LimitReader(src, limit+1))
	if err != nil {
		return n, err
	}
	if n > limit {
		return n, fmt.Errorf("entry exceeds %d bytes", limit)
	}
	return n, nil
}

func writeZipFile(f *zip.File, target string) error {
	if f.UncompressedSize64 > installMaxFileBytes {
		return fmt.Errorf("zip entry %q is larger than %d bytes", f.Name, installMaxFileBytes)
	}
	rc, err := f.Open()
	if err != nil {
		return fmt.Errorf("open zip entry %s: %w", f.Name, err)
	}
	defer rc.Close()

	out, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0644)
	if err != nil {
		return fmt.Errorf("create %s: %w", target, err)
	}
	if _, err := copyLimited(out, rc, installMaxFileBytes); err != nil {
		out.Close()
		return fmt.Errorf("write %s: %w", target, err)
	}
	if err := out.Close(); err != nil {
		return fmt.Errorf("close %s: %w", target, err)
	}
	return nil
}
