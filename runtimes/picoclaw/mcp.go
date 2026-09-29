package picoclaw

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
)

// picoclawConfigPath returns PicoClaw's structure config.json — the parent of the
// workspace dir (HOME=/root → /root/.picoclaw/config.json).
// Secrets live in .security.yml, which we never touch here.
func picoclawConfigPath() string {
	return filepath.Join(filepath.Dir(picoclawWorkspaceDir), "config.json")
}

// WriteMCPEntry upserts tools.mcp.servers.<name> in config.json and restarts the
// gateway so PicoClaw picks up the server.
// tools.mcp.enabled is asserted true; PicoClaw silently ignores servers otherwise.
func (s *PicoclawService) WriteMCPEntry(name string, entry map[string]any) error {
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()

	path := picoclawConfigPath()
	cfg, err := readPicoclawConfig(path)
	if err != nil {
		return err
	}

	applyMCPServerWrite(cfg, name, entry)

	if err := writePicoclawConfig(path, cfg); err != nil {
		return err
	}
	slog.Info("[mcp] wrote tools.mcp.servers entry", "component", "picoclaw", "connector", name)

	if err := restartPicoclawGateway(); err != nil {
		slog.Warn("[mcp] restart gateway after mcp entry write", "component", "picoclaw", "err", err)
	}
	return nil
}

// RemoveMCPEntry deletes tools.mcp.servers.<name> from config.json.
// tools.mcp.enabled is left on — other servers may still be wired, and an enabled-but-empty MCP
// block loads nothing.
func (s *PicoclawService) RemoveMCPEntry(name string) (bool, error) {
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()

	path := picoclawConfigPath()
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, fmt.Errorf("read picoclaw config: %w", err)
	}
	var cfg map[string]any
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return false, fmt.Errorf("parse picoclaw config: %w", err)
	}

	if !applyMCPServerRemove(cfg, name) {
		return false, nil
	}

	if err := writePicoclawConfig(path, cfg); err != nil {
		return false, err
	}
	slog.Info("[mcp] removed tools.mcp.servers entry", "component", "picoclaw", "connector", name)

	if err := restartPicoclawGateway(); err != nil {
		slog.Warn("[mcp] restart gateway after mcp entry remove", "component", "picoclaw", "err", err)
	}
	return true, nil
}

// applyMCPServerWrite upserts tools.mcp.servers.<name> in the decoded config map and
// asserts the tools.mcp.enabled gate (default false).
func applyMCPServerWrite(cfg map[string]any, name string, entry map[string]any) {
	tools := ensurePicoMap(cfg, "tools")
	mcp := ensurePicoMap(tools, "mcp")
	mcp["enabled"] = true // global gate defaults to false — must be on to load any server
	servers := ensurePicoMap(mcp, "servers")
	servers[name] = toPicoclawMCPEntry(entry)
}

// applyMCPServerRemove deletes tools.mcp.servers.<name> from the decoded config map.
func applyMCPServerRemove(cfg map[string]any, name string) bool {
	tools, _ := cfg["tools"].(map[string]any)
	mcp, _ := tools["mcp"].(map[string]any)
	servers, _ := mcp["servers"].(map[string]any)
	if servers == nil {
		return false
	}
	if _, ok := servers[name]; !ok {
		return false
	}
	delete(servers, name)
	return true
}

// toPicoclawMCPEntry copies the canonical OpenClaw-shaped server entry and asserts
// enabled:true (PicoClaw's per-server active flag; also re-enables a previously
// disabled entry on re-write). The type key is kept to avoid PicoClaw's empty-type→sse inference.
func toPicoclawMCPEntry(entry map[string]any) map[string]any {
	out := make(map[string]any, len(entry)+1)
	for k, v := range entry {
		out[k] = v
	}
	out["enabled"] = true
	return out
}

// readPicoclawConfig loads config.json into a generic map.
func readPicoclawConfig(path string) (map[string]any, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read picoclaw config: %w", err)
	}
	cfg := map[string]any{}
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return nil, fmt.Errorf("parse picoclaw config: %w", err)
	}
	return cfg, nil
}

// writePicoclawConfig marshals + atomically writes config.json.
func writePicoclawConfig(path string, cfg map[string]any) error {
	written, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal picoclaw config: %w", err)
	}
	if err := atomicWritePicoFile(path, written, 0o644); err != nil {
		return fmt.Errorf("write picoclaw config: %w", err)
	}
	return nil
}

// ensurePicoMap returns parent[key] as a map[string]any, creating it when absent or
// of the wrong type.
func ensurePicoMap(parent map[string]any, key string) map[string]any {
	if existing, ok := parent[key].(map[string]any); ok && existing != nil {
		return existing
	}
	created := map[string]any{}
	parent[key] = created
	return created
}

// atomicWritePicoFile writes data to a temp file in the same dir then renames it over
// path, so a crash mid-write never leaves a truncated config.json.
// Mirrors hermes.atomicWriteFile (kept local to avoid a cross-package dependency); no chown because
// PicoClaw runs as root.
func atomicWritePicoFile(path string, data []byte, perm os.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), ".picoclaw-*.tmp")
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
