package device

import (
	"fmt"
	"log/slog"
	"strings"

	"go.autonomous.ai/os/system/server/config"
)

// mcpToolEntry builds the mcp.servers entry for a remote MCP tool.
// Custom headers (Authorization, X-API-Key, …) are passed through as-is.
func mcpToolEntry(url string, headers map[string]string) map[string]any {
	h := map[string]any{}
	for k, v := range headers {
		h[k] = v
	}
	return map[string]any{
		"type":    "http",
		"url":     url,
		"headers": h,
	}
}

// mcpToolServerName returns the mcp.servers key for a tool. Prefixed with
// "tool_" to avoid collision with OAuth connector names (notion, github, …).
func mcpToolServerName(name string) string {
	return "tool_" + name
}

// AddMCPTool adds a remote MCP tool to config.json and syncs it to
// openclaw.json so the active runtime picks it up.
func (s *Service) AddMCPTool(tool config.MCPTool) error {
	tool.Name = strings.TrimSpace(tool.Name)
	tool.URL = strings.TrimSpace(tool.URL)
	if tool.Name == "" || tool.URL == "" {
		return fmt.Errorf("mcp tool name and url are required")
	}

	for _, t := range s.config.MCPTools {
		if t.Name == tool.Name {
			return fmt.Errorf("mcp tool %q already exists", tool.Name)
		}
	}

	if err := s.config.WithLockSave(func(c *config.Config) {
		c.MCPTools = append(c.MCPTools, tool)
	}); err != nil {
		return fmt.Errorf("save mcp tool: %w", err)
	}

	if err := s.agentGateway.WriteMCPEntry(mcpToolServerName(tool.Name), mcpToolEntry(tool.URL, tool.Headers)); err != nil {
		slog.Warn("[mcp-tools] write entry failed (config saved, gateway not updated)",
			"component", "device", "tool", tool.Name, "err", err)
		return fmt.Errorf("write mcp entry: %w", err)
	}
	slog.Info("[mcp-tools] added tool", "component", "device", "name", tool.Name, "url", tool.URL)
	return nil
}

// RemoveMCPTool removes a remote MCP tool from config.json and openclaw.json.
func (s *Service) RemoveMCPTool(name string) error {
	name = strings.TrimSpace(name)
	if name == "" {
		return fmt.Errorf("mcp tool name is required")
	}

	found := false
	if err := s.config.WithLockSave(func(c *config.Config) {
		filtered := make([]config.MCPTool, 0, len(c.MCPTools))
		for _, t := range c.MCPTools {
			if t.Name == name {
				found = true
				continue
			}
			filtered = append(filtered, t)
		}
		c.MCPTools = filtered
	}); err != nil {
		return fmt.Errorf("save config: %w", err)
	}

	if !found {
		return fmt.Errorf("mcp tool %q not found", name)
	}

	if _, err := s.agentGateway.RemoveMCPEntry(mcpToolServerName(name)); err != nil {
		slog.Warn("[mcp-tools] remove entry failed", "component", "device", "tool", name, "err", err)
	}
	slog.Info("[mcp-tools] removed tool", "component", "device", "name", name)
	return nil
}

// ListMCPTools returns the configured MCP tools from config.json.
func (s *Service) ListMCPTools() []config.MCPTool {
	tools := s.config.MCPTools
	if tools == nil {
		return []config.MCPTool{}
	}
	return tools
}

// SyncMCPTools ensures all config.json MCP tools are present in openclaw.json.
// Called at startup so tools survive gateway restarts.
func (s *Service) SyncMCPTools() {
	for _, tool := range s.config.MCPTools {
		if err := s.agentGateway.WriteMCPEntry(mcpToolServerName(tool.Name), mcpToolEntry(tool.URL, tool.Headers)); err != nil {
			slog.Warn("[mcp-tools] startup sync failed",
				"component", "device", "tool", tool.Name, "err", err)
			continue
		}
		slog.Info("[mcp-tools] synced tool", "component", "device", "name", tool.Name)
	}
}
