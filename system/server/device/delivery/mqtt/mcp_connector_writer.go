package mqtthandler

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sync"

	"go.autonomous.ai/os/system/domain"
)

// mcpConnectorWriter is the single ConnectorWriter implementation for all
// remote-MCP connectors (Notion, Figma, Asana, …).
type mcpConnectorWriter struct {
	mu      sync.Mutex
	cfg     mcpConnectorConfig
	path    string
	gateway domain.AgentGateway
}

// mcpConnectorConfig parametrizes the per-connector bits. Everything else is
// shared in mcpConnectorWriter's methods.
type mcpConnectorConfig struct {
	// name is the connector code AND the mcp.servers.<name> key (e.g.
	// "notion").
	name string
	// mcpURL is the remote MCP endpoint written into mcp.servers.<name>.url.
	// Used only by the default (http) entry builder.
	mcpURL string
	// header builds the Authorization header value from creds.
	header func(c ConnectorCreds) string
	// tokenFile overrides the token filename. Empty → "<name>_access_tokens.json".
	tokenFile string
	// entry builds the mcp.servers.<name> value from creds.
	entry func(c ConnectorCreds) map[string]any
	// ensureAssets, if set, runs before WriteMCPEntry — used by stdio connectors
	// to drop their wrapper script on disk before the gateway tries to spawn it.
	ensureAssets func() error
}

// bearerAccessToken is the default Authorization builder: "Bearer
// <access_token>".
func bearerAccessToken(c ConnectorCreds) string { return "Bearer " + c.AccessToken }

// newMCPConnectorWriter builds a writer from cfg. configsDir is typically
// `<OpenclawConfigDir>/workspace/configs`.
func newMCPConnectorWriter(cfg mcpConnectorConfig, configsDir string, gw domain.AgentGateway) *mcpConnectorWriter {
	if cfg.header == nil {
		cfg.header = bearerAccessToken
	}
	tokenFile := cfg.tokenFile
	if tokenFile == "" {
		tokenFile = cfg.name + "_access_tokens.json"
	}
	return &mcpConnectorWriter{
		cfg:     cfg,
		path:    filepath.Join(configsDir, tokenFile),
		gateway: gw,
	}
}

// Write persists credentials to <name>_access_tokens.json then writes the
// matching mcp.servers.<name> entry into openclaw.json.
func (w *mcpConnectorWriter) Write(ctx context.Context, creds ConnectorCreds) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	file, err := loadConnectorsFile(w.path)
	if err != nil {
		return fmt.Errorf("%s_writer: token file: %w", w.cfg.name, err)
	}
	file.Connectors[creds.Connector] = connectorEntryFromCreds(creds)
	if err := writeConnectorsFile(w.path, file); err != nil {
		return fmt.Errorf("%s_writer: token file: %w", w.cfg.name, err)
	}

	if w.cfg.ensureAssets != nil {
		if err := w.cfg.ensureAssets(); err != nil {
			return fmt.Errorf("%s_writer: assets: %w", w.cfg.name, err)
		}
	}

	entry := w.buildEntry(creds)
	if err := w.gateway.WriteMCPEntry(w.cfg.name, entry); err != nil {
		return fmt.Errorf("%s_writer: mcp entry: %w", w.cfg.name, err)
	}
	return nil
}

// buildEntry produces the mcp.servers.<name> value: a custom builder when set
// (stdio connectors), otherwise the default hosted-MCP http shape.
func (w *mcpConnectorWriter) buildEntry(creds ConnectorCreds) map[string]any {
	if w.cfg.entry != nil {
		return w.cfg.entry(creds)
	}
	return map[string]any{
		"type": "http",
		"url":  w.cfg.mcpURL,
		"headers": map[string]any{
			"Authorization": w.cfg.header(creds),
		},
	}
}

// Remove deletes the entire per-connector token file and the openclaw.json
// MCP entry.
func (w *mcpConnectorWriter) Remove(ctx context.Context, connector string) (bool, error) {
	w.mu.Lock()
	defer w.mu.Unlock()

	file, err := loadConnectorsFile(w.path)
	if err != nil {
		return false, fmt.Errorf("%s_writer: token file: %w", w.cfg.name, err)
	}
	_, hadToken := file.Connectors[connector]

	// Delete the dedicated token file outright. A missing file is not an error
	// (already gone / never written).
	if err := os.Remove(w.path); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return false, fmt.Errorf("%s_writer: token file: %w", w.cfg.name, err)
	}

	if _, err := w.gateway.RemoveMCPEntry(w.cfg.name); err != nil {
		return hadToken, fmt.Errorf("%s_writer: mcp entry: %w", w.cfg.name, err)
	}
	return hadToken, nil
}

// RefreshableEntries surfaces entries the refresh loop should rotate.
func (w *mcpConnectorWriter) RefreshableEntries() []ConnectorRefreshTarget {
	w.mu.Lock()
	defer w.mu.Unlock()
	file, err := loadConnectorsFile(w.path)
	if err != nil {
		return nil
	}
	out := make([]ConnectorRefreshTarget, 0, 1)
	for code, entry := range file.Connectors {
		if entry.RefreshToken == "" || !entry.Refresh {
			continue
		}
		out = append(out, ConnectorRefreshTarget{
			Connector:    code,
			RefreshToken: entry.RefreshToken,
			ExpiresAt:    entry.ExpiresAt,
		})
	}
	return out
}

// hasEntry reports whether this writer's token file holds an entry for
// connector. Lock-free for the same reason as connectorWriter.hasEntry.
func (w *mcpConnectorWriter) hasEntry(connector string) (bool, error) {
	file, err := loadConnectorsFile(w.path)
	if err != nil {
		return false, err
	}
	_, ok := file.Connectors[connector]
	return ok, nil
}

// loadEntry returns the current on-disk entry for a connector. Satisfies the
// entryLoader interface used by the refresh loop for full-fidelity token merge.
func (w *mcpConnectorWriter) loadEntry(connector string) (ConnectorCreds, bool, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	file, err := loadConnectorsFile(w.path)
	if err != nil {
		return ConnectorCreds{}, false, err
	}
	entry, ok := file.Connectors[connector]
	if !ok {
		return ConnectorCreds{}, false, nil
	}
	return credsFromEntry(connector, entry), true, nil
}
