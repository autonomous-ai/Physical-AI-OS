package skills

import (
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	// StoreBaseURLDefault is the catalog host (override: SKILL_STORE_BASE_URL).
	StoreBaseURLDefault = "https://apiv2.autonomous.ai"

	// storeLocation fills the `location` header every /api/v1 route requires.
	storeLocation = "en-US"

	// StoreDownloadTimeout bounds an archive download.
	StoreDownloadTimeout = 30 * time.Second

	// StoreMaxBytes caps any single response read off the catalog.
	StoreMaxBytes = 16 << 20
)

// StoreBaseURL returns the configured catalog host without a trailing slash.
func StoreBaseURL() string {
	if env := strings.TrimSpace(os.Getenv("SKILL_STORE_BASE_URL")); env != "" {
		return strings.TrimRight(env, "/")
	}
	return StoreBaseURLDefault
}

// StoreGet GETs path from the catalog and returns the body; non-200 is an error.
// Business failures still arrive as HTTP 200 with a non-1 JSON status.
func StoreGet(path string, query url.Values, timeout time.Duration, maxBytes int64) ([]byte, error) {
	u := StoreBaseURL() + path
	if len(query) > 0 {
		u += "?" + query.Encode()
	}
	req, err := http.NewRequest(http.MethodGet, u, nil)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("location", storeLocation)

	client := &http.Client{Timeout: timeout}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("request %s: %w", path, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("skill store returned %s", resp.Status)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxBytes))
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}
	return body, nil
}

// ValidateStoreSkillID rejects an id that could escape the upstream URL path.
func ValidateStoreSkillID(id string) error {
	id = strings.TrimSpace(id)
	if id == "" {
		return fmt.Errorf("skill id is required")
	}
	if strings.ContainsAny(id, "/\\?#") {
		return fmt.Errorf("invalid skill id %q", id)
	}
	return nil
}

// DownloadStoreArchive downloads the `.skill` zip for id into destDir and returns its path.
// Callers own destDir cleanup.
func DownloadStoreArchive(id, destDir string) (string, error) {
	if err := ValidateStoreSkillID(id); err != nil {
		return "", err
	}

	archive, err := StoreGet("/api/v1/agent-skills/"+url.PathEscape(strings.TrimSpace(id))+"/download",
		nil, StoreDownloadTimeout, StoreMaxBytes)
	if err != nil {
		return "", fmt.Errorf("download skill %s: %w", id, err)
	}

	zipPath := filepath.Join(destDir, "skill.zip")
	if err := os.WriteFile(zipPath, archive, 0600); err != nil {
		return "", fmt.Errorf("write temp archive: %w", err)
	}
	return zipPath, nil
}
