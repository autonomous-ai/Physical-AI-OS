package skills

import (
	"archive/zip"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// ExportSkillArchive writes a portable .skill bundle from disk, including binary assets.
func ExportSkillArchive(skillsDir, name, destDir string) (string, error) {
	if err := ValidateSkillName(name); err != nil {
		return "", err
	}
	root := filepath.Join(skillsDir, name)
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		return "", fmt.Errorf("%w: skill %q", ErrSkillNotFound, name)
	}
	out := filepath.Join(destDir, name+".skill")
	f, err := os.OpenFile(out, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0600)
	if err != nil {
		return "", err
	}
	zw := zip.NewWriter(f)
	err = filepath.Walk(root, func(path string, fi os.FileInfo, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if path == root || fi.IsDir() {
			return nil
		}
		if fi.Mode()&os.ModeSymlink != 0 || !fi.Mode().IsRegular() {
			return fmt.Errorf("unsafe skill entry %s", path)
		}
		rel, _ := filepath.Rel(root, path)
		w, err := zw.Create(filepath.ToSlash(filepath.Join(name, rel)))
		if err != nil {
			return err
		}
		r, err := os.Open(path)
		if err != nil {
			return err
		}
		defer r.Close()
		_, err = io.Copy(w, r)
		return err
	})
	if closeErr := zw.Close(); err == nil {
		err = closeErr
	}
	if closeErr := f.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		_ = os.Remove(out)
		return "", err
	}
	return out, nil
}
