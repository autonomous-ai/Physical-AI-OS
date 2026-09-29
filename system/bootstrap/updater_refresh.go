package bootstrap

import (
	"context"
	"crypto/sha256"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path"
	"path/filepath"
)

// software-update is not an OTA component, so bootstrap refreshes it strictly between
// runs (bash reads scripts lazily; a self-overwrite would corrupt a running update).
// Failure is never fatal: the device keeps the updater it already had.

// updaterPath is a var so tests can redirect the install.
var updaterPath = "/usr/local/bin/software-update"

// updaterURLFrom derives the updater URL from the metadata URL.
// Example: {base}/ota/metadata.json -> {base}/software-update
func updaterURLFrom(metadataURL string) (string, error) {
	u, err := url.Parse(metadataURL)
	if err != nil {
		return "", fmt.Errorf("parse metadata url: %w", err)
	}
	if u.Scheme == "" || u.Host == "" {
		return "", fmt.Errorf("metadata url %q is not absolute", metadataURL)
	}
	base := path.Dir(path.Dir(u.Path))
	if base == "." || base == "/" {
		return "", fmt.Errorf("metadata url %q has no namespace to derive from", metadataURL)
	}
	u.Path = path.Join(base, "software-update")
	u.RawQuery = ""
	u.Fragment = ""
	return u.String(), nil
}

// updateInFlight reports whether a force update is installing right now.
func updateInFlight() bool {
	busy := false
	inFlight.Range(func(_, _ any) bool {
		busy = true
		return false
	})
	return busy
}

// refreshUpdater brings software-update up to the published copy; no-op when unchanged.
func (b *Bootstrap) refreshUpdater(ctx context.Context) {
	// The script is executing during an update; replacing it now hits the lazy-read hazard.
	if updateInFlight() {
		return
	}

	current, err := os.ReadFile(updaterPath)
	if err != nil {
		// No updater installed is a provisioning problem; never drop one in.
		return
	}

	src, err := updaterURLFrom(b.cfg.MetadataURL)
	if err != nil {
		slog.Debug("updater refresh: no source url", "component", "bootstrap", "error", err)
		return
	}

	published, err := b.fetchUpdater(ctx, src)
	if err != nil {
		slog.Debug("updater refresh: fetch failed", "component", "bootstrap", "url", src, "error", err)
		return
	}

	if sha256.Sum256(published) == sha256.Sum256(current) {
		return
	}

	if err := installUpdater(ctx, published); err != nil {
		slog.Warn("updater refresh: keeping the existing updater", "component", "bootstrap", "error", err)
		return
	}
	slog.Info("updater refreshed", "component", "bootstrap", "url", src, "bytes", len(published))
}

// fetchUpdater downloads the published script, size-capped against captive portals.
func (b *Bootstrap) fetchUpdater(ctx context.Context, src string) ([]byte, error) {
	const maxUpdaterBytes = 1 << 20 // 1 MiB; the real script is ~40 KB

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, src, nil)
	if err != nil {
		return nil, fmt.Errorf("build request: %w", err)
	}
	// The updater is republished in place; a cached copy would hide the fix.
	req.Header.Set("Cache-Control", "no-cache")

	resp, err := b.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetch %s: %w", src, err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("fetch %s: status %s", src, resp.Status)
	}

	data, err := io.ReadAll(io.LimitReader(resp.Body, maxUpdaterBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", src, err)
	}
	if len(data) > maxUpdaterBytes {
		return nil, fmt.Errorf("fetch %s: larger than %d bytes", src, maxUpdaterBytes)
	}
	if len(data) == 0 {
		return nil, fmt.Errorf("fetch %s: empty body", src)
	}
	return data, nil
}

// installUpdater validates the script and installs it atomically.
// `bash -n` runs before replacing the live file, and the temp file is renamed in the
// same directory so software-update is never half-written.
func installUpdater(ctx context.Context, data []byte) error {
	dir := filepath.Dir(updaterPath)

	tmp, err := os.CreateTemp(dir, ".software-update.new-*")
	if err != nil {
		return fmt.Errorf("create temp: %w", err)
	}
	tmpName := tmp.Name()
	defer func() { _ = os.Remove(tmpName) }()

	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write temp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp: %w", err)
	}
	// Must be executable before the rename publishes it.
	if err := os.Chmod(tmpName, 0o755); err != nil {
		return fmt.Errorf("chmod temp: %w", err)
	}

	if out, err := exec.CommandContext(ctx, "bash", "-n", tmpName).CombinedOutput(); err != nil {
		return fmt.Errorf("downloaded updater failed bash -n: %w: %s", err, out)
	}

	if err := os.Rename(tmpName, updaterPath); err != nil {
		return fmt.Errorf("install: %w", err)
	}
	return nil
}
