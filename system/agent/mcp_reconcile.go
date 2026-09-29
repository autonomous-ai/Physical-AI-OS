package agent

import (
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"path/filepath"

	"github.com/goccy/go-yaml"

	"go.autonomous.ai/os/runtimes/claudecode"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// MCPReconcile clones the previous runtime's MCP servers into the active runtime after a switch.
// Gated by config.MCPAppliedRuntime; only openclaw, hermes and claudecode carry MCP config.
type MCPReconcile struct {
	cfg *config.Config
	gw  domain.AgentGateway
}

// ProvideMCPReconcile is the Wire provider for MCPReconcile.
func ProvideMCPReconcile(cfg *config.Config, gw domain.AgentGateway) *MCPReconcile {
	return &MCPReconcile{cfg: cfg, gw: gw}
}

// Reconcile clones MCP servers if the runtime changed; failures leave the marker for next-boot retry.
func (r *MCPReconcile) Reconcile() {
	current := r.cfg.AgentRuntime
	if current == "" {
		current = domain.AgentRuntimeOpenClaw
	}
	if r.cfg.MCPAppliedRuntime == current {
		return
	}

	// Unset marker: record a baseline only; servers already live in the current runtime.
	if r.cfg.MCPAppliedRuntime == "" {
		if err := r.cfg.WithLockSave(func(c *config.Config) { c.MCPAppliedRuntime = current }); err != nil {
			slog.Warn("mcp reconcile: record baseline failed", "component", "agent", "error", err)
			return
		}
		slog.Info("mcp reconcile: baseline recorded (no clone)", "component", "agent", "runtime", current)
		return
	}

	prev := r.cfg.MCPAppliedRuntime
	entries, err := readMCPEntries(prev, r.cfg)
	if err != nil {
		slog.Error("mcp reconcile: read previous runtime MCP failed; leaving marker for next-boot retry",
			"component", "agent", "from", prev, "to", current, "error", err)
		return
	}
	if len(entries) == 0 {
		slog.Info("mcp reconcile: runtime changed, no MCP servers to clone",
			"component", "agent", "from", prev, "to", current)
		if err := r.cfg.WithLockSave(func(c *config.Config) { c.MCPAppliedRuntime = current }); err != nil {
			slog.Warn("mcp reconcile: persist marker failed", "component", "agent", "error", err)
		}
		return
	}

	slog.Info("mcp reconcile: runtime changed, cloning MCP servers",
		"component", "agent", "from", prev, "to", current, "count", len(entries))
	cloneErr := false
	for name, entry := range entries {
		if err := r.gw.WriteMCPEntry(name, entry); err != nil {
			slog.Error("mcp reconcile: clone entry failed; will retry next boot",
				"component", "agent", "connector", name, "to", current, "error", err)
			cloneErr = true
			continue
		}
		slog.Info("mcp reconcile: cloned MCP server", "component", "agent", "connector", name, "runtime", current)
	}

	// Advance only on a clean pass; the previous runtime's config stays on disk for retry.
	if cloneErr {
		slog.Warn("mcp reconcile: clone error — leaving marker for next-boot retry",
			"component", "agent", "runtime", current)
		return
	}
	if err := r.cfg.WithLockSave(func(c *config.Config) { c.MCPAppliedRuntime = current }); err != nil {
		slog.Warn("mcp reconcile: persist marker failed", "component", "agent", "error", err)
	}
}

// readMCPEntries returns a runtime's MCP servers as canonical (OpenClaw-shaped) entries
// keyed by name; a missing config yields an empty result, not an error.
func readMCPEntries(runtime string, cfg *config.Config) (map[string]map[string]any, error) {
	switch runtime {
	case domain.AgentRuntimeOpenClaw:
		return readOpenclawMCP(filepath.Join(cfg.OpenclawConfigDir, "openclaw.json"))
	case domain.AgentRuntimeHermes:
		return readHermesMCP(filepath.Join(hermesHome, "config.yaml"))
	case domain.AgentRuntimeClaudeCode:
		return claudecode.ReadMCPEntries()
	default:
		return nil, nil
	}
}

// readOpenclawMCP reads openclaw.json mcp.servers (already canonical).
func readOpenclawMCP(path string) (map[string]map[string]any, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	var data map[string]any
	if err := json.Unmarshal(raw, &data); err != nil {
		return nil, err
	}
	mcp, _ := data["mcp"].(map[string]any)
	servers, _ := mcp["servers"].(map[string]any)
	out := make(map[string]map[string]any, len(servers))
	for name, v := range servers {
		if m, ok := v.(map[string]any); ok {
			out[name] = m
		}
	}
	return out, nil
}

// readHermesMCP reads config.yaml mcp_servers normalized to the canonical shape.
func readHermesMCP(path string) (map[string]map[string]any, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, err
	}
	var data map[string]any
	if err := yaml.Unmarshal(raw, &data); err != nil {
		return nil, err
	}
	servers, _ := data["mcp_servers"].(map[string]any)
	out := make(map[string]map[string]any, len(servers))
	for name, v := range servers {
		if m, ok := v.(map[string]any); ok {
			out[name] = hermesToCanonicalMCP(m)
		}
	}
	return out, nil
}

// hermesToCanonicalMCP converts one Hermes entry to the canonical shape (inverse of
// hermes.toHermesMCPEntry): drops `enabled`, adds type "http" to url-bearing servers.
func hermesToCanonicalMCP(m map[string]any) map[string]any {
	out := make(map[string]any, len(m))
	for k, v := range m {
		if k == "enabled" {
			continue
		}
		out[k] = v
	}
	if _, hasType := out["type"]; !hasType {
		if _, hasURL := out["url"]; hasURL {
			out["type"] = "http"
		}
	}
	return out
}
