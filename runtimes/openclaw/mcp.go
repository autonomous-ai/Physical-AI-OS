package openclaw

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
)

// mcpConnectorURLs is the catalog of supported remote-MCP connectors.
var mcpConnectorURLs = map[string]string{
	"notion": "https://mcp.notion.com/mcp",
	"asana":  "https://mcp.asana.com/v2/mcp",
	"linear": "https://mcp.linear.app/mcp",
	"github": "https://api.githubcopilot.com/mcp/",
	"ahrefs": "https://api.ahrefs.com/mcp/mcp",
}

// MCPConnectorURL returns the remote MCP endpoint for a connector code and whether the code is a known connector.
func MCPConnectorURL(name string) (string, bool) {
	url, ok := mcpConnectorURLs[name]
	return url, ok
}

// IsKnownMCPConnector reports whether name is one of the supported connectors.
func IsKnownMCPConnector(name string) bool {
	_, ok := mcpConnectorURLs[name]
	return ok
}

// WriteMCPEntry upserts mcp.servers.<name> in openclaw.json and restarts the gateway.
func (s *OpenclawService) WriteMCPEntry(name string, entry map[string]any) error {
	s.primarySyncMu.Lock()
	defer s.primarySyncMu.Unlock()

	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	configData, err := readOpenclawConfig(configPath)
	if err != nil {
		return err
	}

	mcp := ensureMap(configData, "mcp")
	servers := ensureMap(mcp, "servers")
	servers[name] = entry
	mcp["servers"] = servers
	configData["mcp"] = mcp

	if err := writeOpenclawConfig(configPath, configData); err != nil {
		return err
	}
	slog.Info("[mcp] wrote mcp.servers entry", "component", "openclaw", "connector", name)

	if err := restartOpenclawGateway(); err != nil {
		slog.Warn("[mcp] restart gateway after mcp entry write", "component", "openclaw", "err", err)
	}
	return nil
}

// RemoveMCPEntry deletes mcp.servers.<name> from openclaw.json.
func (s *OpenclawService) RemoveMCPEntry(name string) (bool, error) {
	s.primarySyncMu.Lock()
	defer s.primarySyncMu.Unlock()

	configPath := filepath.Join(s.config.OpenclawConfigDir, "openclaw.json")
	raw, err := os.ReadFile(configPath)
	if err != nil {
		if os.IsNotExist(err) {
			return false, nil
		}
		return false, fmt.Errorf("read openclaw config: %w", err)
	}
	var configData map[string]any
	if err := json.Unmarshal(raw, &configData); err != nil {
		return false, fmt.Errorf("parse openclaw config: %w", err)
	}

	mcp, _ := configData["mcp"].(map[string]any)
	servers, _ := mcp["servers"].(map[string]any)
	if servers == nil {
		return false, nil
	}
	if _, ok := servers[name]; !ok {
		return false, nil
	}
	delete(servers, name)

	if err := writeOpenclawConfig(configPath, configData); err != nil {
		return false, err
	}
	slog.Info("[mcp] removed mcp.servers entry", "component", "openclaw", "connector", name)

	if err := restartOpenclawGateway(); err != nil {
		slog.Warn("[mcp] restart gateway after mcp entry remove", "component", "openclaw", "err", err)
	}
	return true, nil
}

// readOpenclawConfig loads openclaw.json into a generic map.
func readOpenclawConfig(path string) (map[string]any, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read openclaw config: %w", err)
	}
	var configData map[string]any
	if err := json.Unmarshal(raw, &configData); err != nil {
		return nil, fmt.Errorf("parse openclaw config: %w", err)
	}
	return configData, nil
}

// writeOpenclawConfig marshals + atomically writes openclaw.json and chowns it back to the gateway runtime user when running as root.
func writeOpenclawConfig(path string, configData map[string]any) error {
	written, err := json.MarshalIndent(configData, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal openclaw config: %w", err)
	}
	if err := atomicWriteFile(path, written, 0600); err != nil {
		return fmt.Errorf("write openclaw config: %w", err)
	}
	if err := chownRuntimeUserIfRoot(path, openclawRuntimeUser); err != nil {
		slog.Warn("[mcp] chown openclaw config", "component", "openclaw", "err", err)
	}
	return nil
}
