package hermes

import (
	"bytes"
	_ "embed"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
)

//go:embed hooks/os-server-observer/HOOK.yaml
var observerHookYAML []byte

//go:embed hooks/os-server-observer/handler.py
var observerHookHandler []byte

const (
	observerHookDir     = "/root/.hermes/hooks/os-server-observer"
	observerHookURLMark = "__OS_SERVER_TURN_URL__"
	observerHookPort    = 5000 // fallback when config.HttpPort is unset
)

// ensureObserverHook materializes the os-server-observer hook into the Hermes hooks dir.
func (s *HermesService) ensureObserverHook() (bool, error) {
	port := s.config.HttpPort
	if port == 0 {
		port = observerHookPort
	}
	url := fmt.Sprintf("http://127.0.0.1:%d/api/agent/channel-turn", port)
	handler := bytes.ReplaceAll(observerHookHandler, []byte(observerHookURLMark), []byte(url))

	if err := os.MkdirAll(observerHookDir, 0o755); err != nil {
		return false, fmt.Errorf("mkdir hook dir: %w", err)
	}
	changedYAML, err := writeIfChanged(filepath.Join(observerHookDir, "HOOK.yaml"), observerHookYAML)
	if err != nil {
		return false, err
	}
	changedPy, err := writeIfChanged(filepath.Join(observerHookDir, "handler.py"), handler)
	if err != nil {
		return false, err
	}
	if changedYAML || changedPy {
		slog.Info("hermes observer hook materialized", "component", "hermes", "dir", observerHookDir, "url", url)
		return true, nil
	}
	return false, nil
}

// writeIfChanged writes data to path only when it differs from the current content, so a steady boot neither churns the file nor forces a gateway restart.
func writeIfChanged(path string, data []byte) (bool, error) {
	if cur, err := os.ReadFile(path); err == nil && bytes.Equal(cur, data) {
		return false, nil
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		return false, fmt.Errorf("write %s: %w", path, err)
	}
	return true, nil
}
