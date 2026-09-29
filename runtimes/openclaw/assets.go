package openclaw

import (
	_ "embed"
	"fmt"
	"os"
	"path/filepath"
)

// figmaMCPServerJS is the first-party Figma REST MCP stdio server, embedded at build time and dropped onto the device by the figma-api connector writer.
//
//go:embed assets/figma_mcp_server.mjs
var figmaMCPServerJS []byte

const figmaMCPServerFileMode = 0o644

// FigmaMCPServerPath returns the on-disk location of the Figma REST MCP wrapper for a given OpenClaw config dir: <configDir>/workspace/figma-mcp/server.mjs.
func FigmaMCPServerPath(configDir string) string {
	return filepath.Join(configDir, "workspace", "figma-mcp", "server.mjs")
}

// EnsureFigmaMCPServer writes the embedded Figma REST MCP wrapper to disk (overwriting any previous copy so updates ship with the binary) and returns its path.
func EnsureFigmaMCPServer(configDir string) (string, error) {
	path := FigmaMCPServerPath(configDir)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return "", fmt.Errorf("mkdir figma-mcp dir: %w", err)
	}
	if err := os.WriteFile(path, figmaMCPServerJS, figmaMCPServerFileMode); err != nil {
		return "", fmt.Errorf("write figma mcp server: %w", err)
	}
	if err := chownRuntimeUserIfRoot(path, openclawRuntimeUser); err != nil {
		return path, nil
	}
	return path, nil
}
