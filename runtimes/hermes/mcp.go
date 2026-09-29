package hermes

import (
	"fmt"
	"log/slog"
	"os"
	"path/filepath"

	"github.com/goccy/go-yaml"
)

// WriteMCPEntry upserts mcp_servers.<name> in ~/.hermes/config.yaml and restarts the gateway.
func (s *HermesService) WriteMCPEntry(name string, entry map[string]any) error {
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()

	cfg, err := readHermesConfig(hermesConfigYAML)
	if err != nil {
		return err
	}

	servers := ensureYAMLMap(cfg, "mcp_servers")
	servers[name] = toHermesMCPEntry(entry)
	cfg["mcp_servers"] = servers

	if err := writeHermesConfig(hermesConfigYAML, cfg); err != nil {
		return err
	}
	slog.Info("[mcp] wrote mcp_servers entry", "component", "hermes", "connector", name)

	if err := restartHermesGateway(); err != nil {
		slog.Warn("[mcp] restart gateway after mcp entry write", "component", "hermes", "err", err)
	}
	return nil
}

// RemoveMCPEntry deletes mcp_servers.<name> from ~/.hermes/config.yaml.
func (s *HermesService) RemoveMCPEntry(name string) (bool, error) {
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()

	raw, err := os.ReadFile(hermesConfigYAML)
	if err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, fmt.Errorf("read hermes config: %w", err)
	}
	var cfg map[string]any
	if err := yaml.Unmarshal(raw, &cfg); err != nil {
		return false, fmt.Errorf("parse hermes config: %w", err)
	}

	servers, _ := cfg["mcp_servers"].(map[string]any)
	if servers == nil {
		return false, nil
	}
	if _, ok := servers[name]; !ok {
		return false, nil
	}
	delete(servers, name)

	if err := writeHermesConfig(hermesConfigYAML, cfg); err != nil {
		return false, err
	}
	slog.Info("[mcp] removed mcp_servers entry", "component", "hermes", "connector", name)

	if err := restartHermesGateway(); err != nil {
		slog.Warn("[mcp] restart gateway after mcp entry remove", "component", "hermes", "err", err)
	}
	return true, nil
}

// toHermesMCPEntry translates the canonical OpenClaw-shaped server entry into the Hermes mcp_servers schema.
func toHermesMCPEntry(entry map[string]any) map[string]any {
	out := make(map[string]any, len(entry)+1)
	for k, v := range entry {
		if k == "type" {
			continue
		}
		out[k] = v
	}
	out["enabled"] = true
	return out
}

// readHermesConfig loads config.yaml into a generic map.
func readHermesConfig(path string) (map[string]any, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read hermes config: %w", err)
	}
	cfg := map[string]any{}
	if err := yaml.Unmarshal(raw, &cfg); err != nil {
		return nil, fmt.Errorf("parse hermes config: %w", err)
	}
	return cfg, nil
}

// writeHermesConfig marshals + atomically writes config.yaml.
func writeHermesConfig(path string, cfg map[string]any) error {
	written, err := yaml.Marshal(cfg)
	if err != nil {
		return fmt.Errorf("marshal hermes config: %w", err)
	}
	if err := atomicWriteFile(path, written, 0o644); err != nil {
		return fmt.Errorf("write hermes config: %w", err)
	}
	return nil
}

// ensureYAMLMap returns parent[key] as a map[string]any, creating it when absent or of the wrong type.
func ensureYAMLMap(parent map[string]any, key string) map[string]any {
	if existing, ok := parent[key].(map[string]any); ok && existing != nil {
		return existing
	}
	created := map[string]any{}
	parent[key] = created
	return created
}

// atomicWriteFile writes data to a temp file in the same dir then renames it over path, so a crash mid-write never leaves a truncated config.yaml.
func atomicWriteFile(path string, data []byte, perm os.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), ".hermes-*.tmp")
	if err != nil {
		return fmt.Errorf("create temp file: %w", err)
	}
	tmpPath := tmp.Name()
	cleanup := func() { _ = os.Remove(tmpPath) }

	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		cleanup()
		return fmt.Errorf("write temp file: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		cleanup()
		return fmt.Errorf("fsync temp file: %w", err)
	}
	if err := tmp.Close(); err != nil {
		cleanup()
		return fmt.Errorf("close temp file: %w", err)
	}
	if err := os.Chmod(tmpPath, perm); err != nil {
		cleanup()
		return fmt.Errorf("chmod temp file: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		cleanup()
		return fmt.Errorf("rename temp file: %w", err)
	}
	return nil
}
