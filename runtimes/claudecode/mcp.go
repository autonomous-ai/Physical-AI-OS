package claudecode

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
)

// mcpConfigPath returns the workspace .mcp.json location.
func mcpConfigPath() string {
	return filepath.Join(claudecodeWorkspaceDir, ".mcp.json")
}

// WriteMCPEntry upserts mcpServers.<name> in workspace/.mcp.json (created when
// absent) and restarts the bridge so the new connector is live.
func (s *ClaudeCodeService) WriteMCPEntry(name string, entry map[string]any) error {
	if name == "" {
		return fmt.Errorf("mcp entry name is required")
	}
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()

	root, err := readMCPFile()
	if err != nil {
		return err
	}
	servers, _ := root["mcpServers"].(map[string]any)
	if servers == nil {
		servers = map[string]any{}
	}
	servers[name] = entry
	root["mcpServers"] = servers

	if err := writeMCPFile(root); err != nil {
		return err
	}
	slog.Info("mcp entry written", "component", "claudecode", "connector", name, "path", mcpConfigPath())
	if err := restartClaudeCodeGateway(); err != nil {
		return fmt.Errorf("restart claudecode after mcp write: %w", err)
	}
	return nil
}

// RemoveMCPEntry deletes mcpServers.<name>.
func (s *ClaudeCodeService) RemoveMCPEntry(name string) (bool, error) {
	s.mcpMu.Lock()
	defer s.mcpMu.Unlock()

	root, err := readMCPFile()
	if err != nil {
		return false, err
	}
	servers, _ := root["mcpServers"].(map[string]any)
	if _, ok := servers[name]; !ok {
		return false, nil
	}
	delete(servers, name)
	root["mcpServers"] = servers

	if err := writeMCPFile(root); err != nil {
		return false, err
	}
	slog.Info("mcp entry removed", "component", "claudecode", "connector", name)
	if err := restartClaudeCodeGateway(); err != nil {
		return true, fmt.Errorf("restart claudecode after mcp remove: %w", err)
	}
	return true, nil
}

// ReadMCPEntries returns the mcpServers map from workspace/.mcp.json — used by
// MCPReconcile to clone connectors when switching runtimes.
func ReadMCPEntries() (map[string]map[string]any, error) {
	root, err := readMCPFile()
	if err != nil {
		return nil, err
	}
	servers, _ := root["mcpServers"].(map[string]any)
	out := map[string]map[string]any{}
	for name, v := range servers {
		if entry, ok := v.(map[string]any); ok {
			out[name] = entry
		}
	}
	return out, nil
}

// readMCPFile parses .mcp.json into a generic map; a missing file yields an
// empty root so first-write creates it.
func readMCPFile() (map[string]any, error) {
	data, err := os.ReadFile(mcpConfigPath())
	if err != nil {
		if os.IsNotExist(err) {
			return map[string]any{}, nil
		}
		return nil, fmt.Errorf("read %s: %w", mcpConfigPath(), err)
	}
	var root map[string]any
	if err := json.Unmarshal(data, &root); err != nil {
		return nil, fmt.Errorf("parse %s: %w", mcpConfigPath(), err)
	}
	return root, nil
}

// writeMCPFile writes .mcp.json atomically (tmp + rename).
func writeMCPFile(root map[string]any) error {
	out, err := json.MarshalIndent(root, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal .mcp.json: %w", err)
	}
	dir := filepath.Dir(mcpConfigPath())
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("mkdir %s: %w", dir, err)
	}
	tmp, err := os.CreateTemp(dir, ".mcp.*.tmp")
	if err != nil {
		return fmt.Errorf("create tmp: %w", err)
	}
	tmpPath := tmp.Name()
	if _, err := tmp.Write(append(out, '\n')); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return fmt.Errorf("write tmp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("close tmp: %w", err)
	}
	if err := os.Rename(tmpPath, mcpConfigPath()); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("rename: %w", err)
	}
	return nil
}
