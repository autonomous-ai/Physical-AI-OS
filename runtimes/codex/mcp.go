package codex

import (
	"fmt"
	"log/slog"
	"os"
	"path/filepath"

	"github.com/pelletier/go-toml/v2"
)

// presync.sh regenerates the config head on every switch but preserves everything from the first
// `[mcp_servers` line down, so the two owners do not collide; concurrent connector.set writes are
// serialized under mcpMu.

// codexConfigPath returns Codex's config.toml (CODEX_HOME=/root/.codex).
func codexConfigPath() string {
	return filepath.Join(codexHome, "config.toml")
}

// WriteMCPEntry upserts mcp_servers.<name> in config.toml and restarts the
// gateway so the next `codex exec` picks the server up.
func (s *CodexService) WriteMCPEntry(name string, entry map[string]any) error {
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()

	path := codexConfigPath()
	cfg, err := readCodexConfig(path)
	if err != nil {
		return err
	}

	servers := ensureCodexMap(cfg, "mcp_servers")
	servers[name] = toCodexMCPEntry(entry)

	if err := writeCodexConfig(path, cfg); err != nil {
		return err
	}
	slog.Info("[mcp] wrote mcp_servers entry", "component", "codex", "connector", name)

	if err := restartCodexGateway(); err != nil {
		slog.Warn("[mcp] restart gateway after mcp entry write", "component", "codex", "err", err)
	}
	return nil
}

// RemoveMCPEntry deletes mcp_servers.<name> from config.toml.
func (s *CodexService) RemoveMCPEntry(name string) (bool, error) {
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()

	path := codexConfigPath()
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, fmt.Errorf("read codex config: %w", err)
	}
	cfg := map[string]any{}
	if err := toml.Unmarshal(raw, &cfg); err != nil {
		return false, fmt.Errorf("parse codex config: %w", err)
	}

	servers, _ := cfg["mcp_servers"].(map[string]any)
	if servers == nil {
		return false, nil
	}
	if _, ok := servers[name]; !ok {
		return false, nil
	}
	delete(servers, name)

	if err := writeCodexConfig(path, cfg); err != nil {
		return false, err
	}
	slog.Info("[mcp] removed mcp_servers entry", "component", "codex", "connector", name)

	if err := restartCodexGateway(); err != nil {
		slog.Warn("[mcp] restart gateway after mcp entry remove", "component", "codex", "err", err)
	}
	return true, nil
}

// toCodexMCPEntry translates the canonical OpenClaw-shaped server entry into
// Codex's config.toml shape.
func toCodexMCPEntry(entry map[string]any) map[string]any {
	out := make(map[string]any, len(entry))
	for k, v := range entry {
		switch k {
		case "type":
		case "headers":
			out["http_headers"] = v
		default:
			out[k] = v
		}
	}
	return out
}

// readCodexConfig loads config.toml into a generic map.
func readCodexConfig(path string) (map[string]any, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read codex config: %w", err)
	}
	cfg := map[string]any{}
	if err := toml.Unmarshal(raw, &cfg); err != nil {
		return nil, fmt.Errorf("parse codex config: %w", err)
	}
	return cfg, nil
}

// writeCodexConfig marshals + atomically writes config.toml.
func writeCodexConfig(path string, cfg map[string]any) error {
	written, err := toml.Marshal(cfg)
	if err != nil {
		return fmt.Errorf("marshal codex config: %w", err)
	}
	if err := atomicWriteCodexFile(path, written, 0o644); err != nil {
		return fmt.Errorf("write codex config: %w", err)
	}
	return nil
}

// ensureCodexMap returns parent[key] as a map[string]any, creating it when
// absent or of the wrong type.
func ensureCodexMap(parent map[string]any, key string) map[string]any {
	if existing, ok := parent[key].(map[string]any); ok && existing != nil {
		return existing
	}
	created := map[string]any{}
	parent[key] = created
	return created
}

// atomicWriteCodexFile writes data to a temp file in the same dir then renames
// it over path, so a crash mid-write never leaves a truncated config.toml.
func atomicWriteCodexFile(path string, data []byte, perm os.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), ".codex-*.tmp")
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
