package device

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// soulDownloadTimeout bounds an http(s) soul_ref fetch. Onboarding runs on the
// boot path, so a hung artifact host must not hold the gateway back.
const soulDownloadTimeout = 30 * time.Second

// ResolveSoul returns the soul text named by soul_ref in robots/<deviceType>/ROBOT.md
// (http(s) URL or path relative to that dir). No soul_ref returns hasSoul=false;
// a declared soul_ref that fails to resolve is an error.
func ResolveSoul(deviceType string) (content []byte, hasSoul bool, err error) {
	ref := SoulRef(deviceType)
	if ref == "" {
		return nil, false, nil
	}
	if strings.HasPrefix(ref, "http://") || strings.HasPrefix(ref, "https://") {
		b, derr := downloadSoul(ref)
		if derr != nil {
			return nil, false, fmt.Errorf("download soul_ref %q: %w", ref, derr)
		}
		return b, true, nil
	}
	// Reject unsupported schemes instead of treating them as a relative path.
	if strings.Contains(ref, "://") {
		return nil, false, fmt.Errorf("unsupported soul_ref scheme: %q (use http(s):// or a path)", ref)
	}
	path := filepath.Join(DevicesDir(), deviceType, ref)
	b, rerr := os.ReadFile(path)
	if rerr != nil {
		return nil, false, fmt.Errorf("read soul_ref %q: %w", path, rerr)
	}
	return b, true, nil
}

// downloadSoul fetches a soul artifact named by an http(s) soul_ref.
func downloadSoul(url string) ([]byte, error) {
	client := &http.Client{Timeout: soulDownloadTimeout}
	resp, err := client.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("status %d", resp.StatusCode)
	}
	return io.ReadAll(resp.Body)
}
