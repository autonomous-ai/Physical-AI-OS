package skills

import (
	"archive/zip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// FetchSkillVersions returns skill name -> version from OTA metadata; (nil, nil) if unprovisioned.
func FetchSkillVersions(otaMetadataURL string) (map[string]string, error) {
	if strings.TrimSpace(otaMetadataURL) == "" {
		return nil, nil
	}
	resp, err := http.Get(otaMetadataURL)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	var meta map[string]json.RawMessage
	if err := json.Unmarshal(body, &meta); err != nil {
		return nil, err
	}
	raw, ok := meta["skills"]
	if !ok {
		return nil, nil
	}
	var skillMap map[string]struct {
		Version string `json:"version"`
	}
	if err := json.Unmarshal(raw, &skillMap); err != nil {
		return nil, err
	}
	result := make(map[string]string, len(skillMap))
	for name, v := range skillMap {
		result[name] = v.Version
	}
	return result, nil
}

// DownloadToTempFile fetches url into a temp file; the caller removes it.
func DownloadToTempFile(url, pattern string) (string, error) {
	client := &http.Client{Timeout: 60 * time.Second}
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Cache-Control", "no-cache")
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	f, err := os.CreateTemp("", pattern)
	if err != nil {
		return "", err
	}
	if _, err := io.Copy(f, resp.Body); err != nil {
		f.Close()
		os.Remove(f.Name())
		return "", err
	}
	if err := f.Close(); err != nil {
		os.Remove(f.Name())
		return "", err
	}
	return f.Name(), nil
}

// FolderHash returns a deterministic sha256 of dir's paths and bytes; "" if dir is missing.
func FolderHash(dir string) (string, error) {
	h := sha256.New()
	err := filepath.Walk(dir, func(path string, info os.FileInfo, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		rel, err := filepath.Rel(dir, path)
		if err != nil {
			return err
		}
		h.Write([]byte(rel))
		h.Write([]byte{0})
		if info.IsDir() {
			return nil
		}
		f, err := os.Open(path)
		if err != nil {
			return err
		}
		defer f.Close()
		if _, err := io.Copy(h, f); err != nil {
			return err
		}
		h.Write([]byte{0})
		return nil
	})
	if err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// ExtractSkillZip atomically replaces targetDir via <targetDir>.new; on failure
// targetDir is untouched.
func ExtractSkillZip(archivePath, targetDir string) error {
	tmpDir := targetDir + ".new"
	if err := os.RemoveAll(tmpDir); err != nil {
		return fmt.Errorf("clean tmp dir: %w", err)
	}
	if err := os.MkdirAll(tmpDir, 0755); err != nil {
		return fmt.Errorf("mkdir tmp dir: %w", err)
	}

	if err := unzipInto(archivePath, tmpDir); err != nil {
		os.RemoveAll(tmpDir)
		return err
	}

	if err := os.RemoveAll(targetDir); err != nil {
		os.RemoveAll(tmpDir)
		return fmt.Errorf("remove old target: %w", err)
	}
	if err := os.Rename(tmpDir, targetDir); err != nil {
		_ = os.RemoveAll(tmpDir)
		return fmt.Errorf("rename %s → %s: %w", tmpDir, targetDir, err)
	}
	return nil
}

// unzipInto extracts archivePath into dest with a traversal guard and forced 0644/0755 perms.
func unzipInto(archivePath, dest string) error {
	r, err := zip.OpenReader(archivePath)
	if err != nil {
		return fmt.Errorf("open zip %s: %w", archivePath, err)
	}
	defer r.Close()

	cleanDest, err := filepath.Abs(dest)
	if err != nil {
		return fmt.Errorf("abs dest: %w", err)
	}
	cleanDest = filepath.Clean(cleanDest) + string(os.PathSeparator)

	for _, f := range r.File {
		if filepath.IsAbs(f.Name) || strings.Contains(f.Name, "..") {
			return fmt.Errorf("invalid zip entry %q", f.Name)
		}
		target := filepath.Join(dest, f.Name)
		absTarget, err := filepath.Abs(target)
		if err != nil {
			return fmt.Errorf("abs target %s: %w", target, err)
		}
		if !strings.HasPrefix(absTarget+string(os.PathSeparator), cleanDest) &&
			absTarget+string(os.PathSeparator) != cleanDest {
			return fmt.Errorf("zip entry escapes dest: %q", f.Name)
		}

		if f.FileInfo().IsDir() {
			if err := os.MkdirAll(target, 0755); err != nil {
				return fmt.Errorf("mkdir %s: %w", target, err)
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
			return fmt.Errorf("mkdir parent %s: %w", target, err)
		}

		rc, err := f.Open()
		if err != nil {
			return fmt.Errorf("open zip entry %s: %w", f.Name, err)
		}
		out, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0644)
		if err != nil {
			rc.Close()
			return fmt.Errorf("create %s: %w", target, err)
		}
		if _, err := io.Copy(out, rc); err != nil {
			rc.Close()
			out.Close()
			return fmt.Errorf("write %s: %w", target, err)
		}
		rc.Close()
		if err := out.Close(); err != nil {
			return fmt.Errorf("close %s: %w", target, err)
		}
	}
	return nil
}
