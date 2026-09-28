package opencode

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
)

// presync.sh regenerates the provider/model head on every switch but preserves the existing "mcp"
// object, so the two owners do not collide; concurrent connector.set writes are serialized under
// mcpMu.

// opencodeConfigPath returns opencode's global config.json (XDG).
func opencodeConfigPath() string {
	return opencodeConfigJSON
}

// WriteMCPEntry upserts mcp.<name> in opencode.json and restarts the gateway so
// the next `opencode run` picks the server up.
func (s *OpenCodeService) WriteMCPEntry(name string, entry map[string]any) error {
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()

	path := opencodeConfigPath()
	cfg, err := readOpenCodeConfig(path)
	if err != nil {
		return err
	}

	servers := ensureOpenCodeMap(cfg, "mcp")
	servers[name] = toOpenCodeMCPEntry(entry)

	if err := writeOpenCodeConfig(path, cfg); err != nil {
		return err
	}
	slog.Info("[mcp] wrote mcp entry", "component", "opencode", "connector", name)

	if err := restartOpenCodeGateway(); err != nil {
		slog.Warn("[mcp] restart gateway after mcp entry write", "component", "opencode", "err", err)
	}
	return nil
}

// RemoveMCPEntry deletes mcp.<name> from opencode.json.
func (s *OpenCodeService) RemoveMCPEntry(name string) (bool, error) {
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()

	path := opencodeConfigPath()
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, fmt.Errorf("read opencode config: %w", err)
	}
	cfg := map[string]any{}
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return false, fmt.Errorf("parse opencode config: %w", err)
	}

	servers, _ := cfg["mcp"].(map[string]any)
	if servers == nil {
		return false, nil
	}
	if _, ok := servers[name]; !ok {
		return false, nil
	}
	delete(servers, name)

	if err := writeOpenCodeConfig(path, cfg); err != nil {
		return false, err
	}
	slog.Info("[mcp] removed mcp entry", "component", "opencode", "connector", name)

	if err := restartOpenCodeGateway(); err != nil {
		slog.Warn("[mcp] restart gateway after mcp entry remove", "component", "opencode", "err", err)
	}
	return true, nil
}

// toOpenCodeMCPEntry translates the canonical OpenClaw-shaped server entry into
// opencode's opencode.json "mcp" shape.
func toOpenCodeMCPEntry(entry map[string]any) map[string]any {
	if url, ok := entry["url"]; ok {
		out := map[string]any{"type": "remote", "url": url, "enabled": true}
		if h, ok := entry["headers"]; ok {
			out["headers"] = h
		}
		return out
	}
	out := map[string]any{"type": "local", "enabled": true}
	cmd := []any{}
	if c, ok := entry["command"].(string); ok && c != "" {
		cmd = append(cmd, c)
	}
	switch a := entry["args"].(type) {
	case []any:
		cmd = append(cmd, a...)
	case []string:
		for _, v := range a {
			cmd = append(cmd, v)
		}
	}
	out["command"] = cmd
	if env, ok := entry["env"]; ok {
		out["environment"] = env
	}
	return out
}

// readOpenCodeConfig loads opencode.json into a generic map.
func readOpenCodeConfig(path string) (map[string]any, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read opencode config: %w", err)
	}
	cfg := map[string]any{}
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return nil, fmt.Errorf("parse opencode config: %w", err)
	}
	return cfg, nil
}

// writeOpenCodeConfig marshals + atomically writes opencode.json (2-space
// indent, matching presync's jq output).
func writeOpenCodeConfig(path string, cfg map[string]any) error {
	written, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal opencode config: %w", err)
	}
	written = append(written, '\n')
	if err := atomicWriteOpenCodeFile(path, written, 0o644); err != nil {
		return fmt.Errorf("write opencode config: %w", err)
	}
	return nil
}

// ensureOpenCodeMap returns parent[key] as a map[string]any, creating it when
// absent or of the wrong type.
func ensureOpenCodeMap(parent map[string]any, key string) map[string]any {
	if existing, ok := parent[key].(map[string]any); ok && existing != nil {
		return existing
	}
	created := map[string]any{}
	parent[key] = created
	return created
}

// atomicWriteOpenCodeFile writes data to a temp file in the same dir then renames
// it over path, so a crash mid-write never leaves a truncated opencode.json.
func atomicWriteOpenCodeFile(path string, data []byte, perm os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("mkdir config dir: %w", err)
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".opencode-*.tmp")
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
