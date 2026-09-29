package device

import (
	"bytes"
	"fmt"
	"os"
	"path/filepath"

	"go.autonomous.ai/os/system/lib/runtimereg"
)

// embeddedInstallerDir holds embedded backend installers; switch_runtime.sh
// checks it before the CDN. Keep in sync with switch_runtime.sh.
const embeddedInstallerDir = "/usr/local/lib/os-runtimes"

// materializeInstaller writes the runtime's embedded installer to
// embeddedInstallerDir/<runtime>/install.sh (idempotent; nil if none embedded).
func materializeInstaller(runtime string) error {
	script, ok := runtimereg.Get(runtime)
	if !ok {
		return nil
	}
	dir := filepath.Join(embeddedInstallerDir, runtime)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("mkdir %s: %w", dir, err)
	}
	p := filepath.Join(dir, "install.sh")
	if cur, err := os.ReadFile(p); err == nil && bytes.Equal(cur, script) {
		return nil
	}
	if err := os.WriteFile(p, script, 0o755); err != nil {
		return fmt.Errorf("write %s: %w", p, err)
	}
	return nil
}

// presyncHookPath is where switch_runtime.sh looks for a backend's optional
// pre-start hook (HOOK="/usr/local/bin/runtime-${NEW}-presync"). Keep in sync.
func presyncHookPath(runtime string) string {
	return filepath.Join("/usr/local/bin", "runtime-"+runtime+"-presync")
}

// materializePresync writes the runtime's embedded pre-start hook to
// /usr/local/bin/runtime-<runtime>-presync (idempotent; nil if none embedded).
// Done here, not in install.sh, so an os-server OTA refreshes the hook on disk.
func materializePresync(runtime string) error {
	script, ok := runtimereg.GetPresync(runtime)
	if !ok {
		return nil
	}
	p := presyncHookPath(runtime)
	if cur, err := os.ReadFile(p); err == nil && bytes.Equal(cur, script) {
		return nil
	}
	if err := os.WriteFile(p, script, 0o755); err != nil {
		return fmt.Errorf("write %s: %w", p, err)
	}
	return nil
}

// materializeReadiness writes the target runtime's optional readiness probe to
// embeddedInstallerDir/<runtime>/ready. switch_runtime.sh invokes that probe
// only when its caller requested a readiness-confirmed switch.
func materializeReadiness(runtime string) error {
	script, ok := runtimereg.GetReadiness(runtime)
	if !ok {
		return nil
	}
	dir := filepath.Join(embeddedInstallerDir, runtime)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("mkdir %s: %w", dir, err)
	}
	p := filepath.Join(dir, "ready")
	if cur, err := os.ReadFile(p); err == nil && bytes.Equal(cur, script) {
		return nil
	}
	if err := os.WriteFile(p, script, 0o755); err != nil {
		return fmt.Errorf("write %s: %w", p, err)
	}
	return nil
}
