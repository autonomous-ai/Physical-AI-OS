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
// remote-MCP connectors (Notion, Figma, Asana, …). The 90% shared logic —
// persist a per-connector token file + write the mcp.servers.<name> entry into
// openclaw.json (and restart the gateway) — lives here once. The 10% that
// differs between connectors is supplied via mcpConnectorConfig.
//
// The openclaw.json side-effect goes through domain.AgentGateway (lamp always
// runs OpenClaw, so the entry write always restarts the gateway — there is no
// hermes/ActiveAgent branch to honour).
type mcpConnectorWriter struct {
	mu      sync.Mutex
	cfg     mcpConnectorConfig
	path    string
	gateway domain.AgentGateway
}

// mcpConnectorConfig parametrizes the per-connector bits. Everything else is
// shared in mcpConnectorWriter's methods.
type mcpConnectorConfig struct {
	// name is the connector code AND the mcp.servers.<name> key (e.g. "notion").
	// Also used for the error-message prefix ("<name>_writer: …") and to derive
	// the token filename when tokenFile is empty.
	name string
	// mcpURL is the remote MCP endpoint written into mcp.servers.<name>.url.
	// Used only by the default (http) entry builder.
	mcpURL string
	// header builds the Authorization header value from creds. Defaults to
	// "Bearer " + AccessToken (bearerAccessToken) when nil. Used only by the
	// default (http) entry builder; the lone caller (figma-api) supplies a custom
	// entry instead, so this path is currently dormant.
	header func(c ConnectorCreds) string
	// tokenFile overrides the token filename. Empty → "<name>_access_tokens.json".
	tokenFile string
	// entry builds the mcp.servers.<name> value from creds. nil → the default
	// hosted-MCP http entry ({type:"http", url, headers.Authorization}). A
	// stdio connector (e.g. figma-api) supplies a builder returning
	// {command, args, env} so OpenClaw spawns a local wrapper instead.
	entry func(c ConnectorCreds) map[string]any
	// ensureAssets, if set, runs before WriteMCPEntry — used by stdio connectors
	// to drop their wrapper script on disk before the gateway tries to spawn it.
	ensureAssets func() error
}

// bearerAccessToken is the default Authorization builder: "Bearer <access_token>".
// Used by OAuth connectors whose token lands in the access_token field. (The
// generic connectorWriter handles static-API-key connectors like Ahrefs via its
// own bearer_api_key header style — see connector_writer_generic.go.)
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
// matching mcp.servers.<name> entry into openclaw.json. If the second step
// fails the credentials are still on disk — the refresh loop / a later
// connector.set will retry the openclaw side without re-fetching tokens.
//
// Error prefixes are stable ("<name>_writer: token file: …" / "<name>_writer:
// mcp entry: …") so the handler/refresh-loop build consistent failure strings.
func (w *mcpConnectorWriter) Write(ctx context.Context, creds ConnectorCreds) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	// 1. Persist token file (own schema, isolated from connectors.json).
	file, err := loadConnectorsFile(w.path)
	if err != nil {
		return fmt.Errorf("%s_writer: token file: %w", w.cfg.name, err)
	}
	file.Connectors[creds.Connector] = connectorEntryFromCreds(creds)
	if err := writeConnectorsFile(w.path, file); err != nil {
		return fmt.Errorf("%s_writer: token file: %w", w.cfg.name, err)
	}

	// 2. Drop any wrapper assets the entry depends on (stdio connectors) before
	// the gateway is asked to spawn them.
	if w.cfg.ensureAssets != nil {
		if err := w.cfg.ensureAssets(); err != nil {
			return fmt.Errorf("%s_writer: assets: %w", w.cfg.name, err)
		}
	}

	// 3. Push the mcp.servers.<name> entry into openclaw.json + restart the
	// gateway. Default = hosted-MCP http entry; stdio connectors override via
	// cfg.entry.
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

// Remove deletes the entire per-connector token file and the openclaw.json MCP
// entry. Each MCP connector owns a dedicated <code>_access_tokens.json, so the
// whole file is removed rather than rewritten empty. Returns removed=true when
// the token entry was present before deletion.
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
//
// Universal rule: refresh eligibility is owned by the backend via the
// connector.set `refresh` flag (persisted as entry.Refresh). An entry is
// refreshed only when it carries BOTH a refresh_token AND refresh:true.
// Absent/false flag → device never auto-refreshes that entry, regardless of
// connector. The BE is the single source of truth for rotation.
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
// connector — see connectorWriter.hasEntry for why it is lock-free (Write holds
// w.mu across the gateway restart) and why that is safe (the file is replaced
// by tmp+rename, or deleted outright by Remove; w.path never changes).
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
