package mqtthandler

import (
	"context"
	"fmt"
	"path/filepath"
	"regexp"
	"strings"
	"sync"

	"go.autonomous.ai/os/runtimes/openclaw"
)

// validConnectorCode bounds an untrusted connector code to [a-z0-9_-] before
// it becomes a filename or mcp.servers key (no "/" or ".": no path traversal).
var validConnectorCode = regexp.MustCompile(`^[a-z0-9_-]{1,64}$`)

// Credential-map keys the backend sets in the connector's connector-auth
// `extra`.
const (
	credentialMCPURL        = "mcp_url"         // remote MCP endpoint; present → MCP connector
	credentialMCPAuthHeader = "mcp_auth_header" // see authHeader* below
)

// Authorization-header styles for the mcp.servers.<code> entry.
const (
	authHeaderBearerAccessToken = "bearer_access_token" // "Bearer " + access_token (default)
	authHeaderBearerAPIKey      = "bearer_api_key"      // "Bearer " + api_key (static-key connectors, e.g. ahrefs)
	// authHeaderCustomPrefix marks a raw custom header: "header:<Name>" sends
	// "<Name>: <token>" with no Bearer prefix (e.g. "header:X-Figma-Token").
	authHeaderCustomPrefix = "header:"
)

// mcpEntryWriter is the subset of the agent gateway the connector writer
// needs.
type mcpEntryWriter interface {
	WriteMCPEntry(name string, entry map[string]any) error
	RemoveMCPEntry(name string) (bool, error)
}

// mcpRouting is one fallback-table row: where an already-shipping MCP connector
// is wired when its payload doesn't (yet) carry mcp_url/mcp_auth_header.
type mcpRouting struct {
	url        string
	authHeader string
}

// connectorWriter is the single, data-driven ConnectorWriter for every
// connector. One mutex guards all per-connector files.
type connectorWriter struct {
	mu      sync.Mutex
	dir     string
	gateway mcpEntryWriter
	// fallback maps connector code → routing for the connectors implemented
	// before mcp_url/mcp_auth_header were carried on the wire.
	fallback map[string]mcpRouting
	// reserved is the set of connector codes owned by a special writer
	// (handler.specialConnectorWriters).
	reserved map[string]bool
}

// newConnectorWriter builds the writer.
func newConnectorWriter(configsDir string, gw mcpEntryWriter, reserved map[string]bool) *connectorWriter {
	fallback := make(map[string]mcpRouting, len(mcpConnectorSpecs))
	for _, sp := range mcpConnectorSpecs {
		url, ok := openclaw.MCPConnectorURL(sp.name)
		if !ok {
			continue
		}
		style := authHeaderBearerAccessToken
		if sp.apiKey {
			style = authHeaderBearerAPIKey
		}
		fallback[sp.name] = mcpRouting{url: url, authHeader: style}
	}
	return &connectorWriter{
		dir:      configsDir,
		gateway:  gw,
		reserved: reserved,
		fallback: fallback,
	}
}

// pathFor is the per-connector token file; rejects codes outside the safe
// charset.
func (w *connectorWriter) pathFor(connector string) (string, error) {
	if !validConnectorCode.MatchString(connector) {
		return "", fmt.Errorf("invalid connector code %q", connector)
	}
	return filepath.Join(w.dir, connector+"_access_tokens.json"), nil
}

// resolveRouting decides whether this connector is an MCP server and how to
// build its Authorization header.
func (w *connectorWriter) resolveRouting(creds ConnectorCreds) mcpRouting {
	if url := strings.TrimSpace(creds.Credentials[credentialMCPURL]); url != "" {
		return mcpRouting{
			url:        url,
			authHeader: strings.TrimSpace(creds.Credentials[credentialMCPAuthHeader]),
		}
	}
	if r, ok := w.fallback[creds.Connector]; ok {
		return r
	}
	return mcpRouting{}
}

// buildAuthHeader renders the Authorization value for the mcp.servers entry.
// Deprecated: use connectorAuthHeader which also returns the header name and
// raw token, enabling custom header keys (e.g. "header:X-Figma-Token").
func buildAuthHeader(style string, creds ConnectorCreds) string {
	if style == authHeaderBearerAPIKey {
		return "Bearer " + creds.APIKey
	}
	return "Bearer " + creds.AccessToken
}

// connectorAuthHeader renders how a connector's token is presented as an HTTP
// header for the mcp.servers entry, from the descriptor + creds.
func connectorAuthHeader(descriptor string, creds ConnectorCreds) (name, value, token string) {
	switch {
	case descriptor == authHeaderBearerAPIKey:
		return "Authorization", "Bearer " + creds.APIKey, creds.APIKey
	case strings.HasPrefix(descriptor, authHeaderCustomPrefix):
		hdr := strings.TrimSpace(strings.TrimPrefix(descriptor, authHeaderCustomPrefix))
		// Whichever field is populated is the credential — this avoids
		// coupling the token source to a specific auth_type string.
		tok := creds.APIKey
		if tok == "" {
			tok = creds.AccessToken
		}
		if hdr == "" || strings.EqualFold(hdr, "Authorization") {
			return "Authorization", "Bearer " + tok, tok
		}
		return hdr, tok, tok
	default: // bearer_access_token / "" / unknown
		return "Authorization", "Bearer " + creds.AccessToken, creds.AccessToken
	}
}

// Write persists the token file then, when the connector resolves to an MCP
// server, upserts mcp.servers.<code> in openclaw.json.
func (w *connectorWriter) Write(ctx context.Context, creds ConnectorCreds) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	path, err := w.pathFor(creds.Connector)
	if err != nil {
		return err
	}
	file, err := loadConnectorsFile(path)
	if err != nil {
		return fmt.Errorf("connector %s: token file: %w", creds.Connector, err)
	}
	file.Connectors[creds.Connector] = connectorEntryFromCreds(creds)
	if err := writeConnectorsFile(path, file); err != nil {
		return fmt.Errorf("connector %s: token file: %w", creds.Connector, err)
	}

	routing := w.resolveRouting(creds)
	if routing.url == "" {
		return nil
	}
	hdrName, hdrValue, _ := connectorAuthHeader(routing.authHeader, creds)
	entry := map[string]any{
		"type": "http",
		"url":  routing.url,
		"headers": map[string]any{
			hdrName: hdrValue,
		},
	}
	if err := w.gateway.WriteMCPEntry(creds.Connector, entry); err != nil {
		return fmt.Errorf("connector %s: mcp entry: %w", creds.Connector, err)
	}
	return nil
}

// Remove deletes the token-file entry and the openclaw.json MCP entry (if
// any).
func (w *connectorWriter) Remove(ctx context.Context, connector string) (bool, error) {
	w.mu.Lock()
	defer w.mu.Unlock()

	path, err := w.pathFor(connector)
	if err != nil {
		return false, err
	}
	file, err := loadConnectorsFile(path)
	if err != nil {
		return false, fmt.Errorf("connector %s: token file: %w", connector, err)
	}
	_, had := file.Connectors[connector]
	if had {
		delete(file.Connectors, connector)
		if err := writeConnectorsFile(path, file); err != nil {
			return false, fmt.Errorf("connector %s: token file: %w", connector, err)
		}
	}
	if _, err := w.gateway.RemoveMCPEntry(connector); err != nil {
		return had, fmt.Errorf("connector %s: mcp entry: %w", connector, err)
	}
	return had, nil
}

// RefreshableEntries scans every per-connector token file for entries the
// refresh loop should rotate.
func (w *connectorWriter) RefreshableEntries() []ConnectorRefreshTarget {
	w.mu.Lock()
	defer w.mu.Unlock()

	matches, err := filepath.Glob(filepath.Join(w.dir, "*_access_tokens.json"))
	if err != nil {
		return nil
	}
	var out []ConnectorRefreshTarget
	for _, path := range matches {
		file, err := loadConnectorsFile(path)
		if err != nil {
			continue
		}
		for code, entry := range file.Connectors {
			// Codes owned by a special writer are refreshed by that writer, not
			// here — skip so we don't re-Write them in the wrong (http) shape.
			if w.reserved[code] {
				continue
			}
			if entry.RefreshToken == "" || !entry.Refresh {
				continue
			}
			out = append(out, ConnectorRefreshTarget{
				Connector:    code,
				RefreshToken: entry.RefreshToken,
				ExpiresAt:    entry.ExpiresAt,
			})
		}
	}
	return out
}

// hasEntry reports whether connector's token file holds an entry for it.
// Deliberately lock-free: Write holds w.mu across a 30-60s gateway restart;
// safe because files are replaced via tmp+rename.
func (w *connectorWriter) hasEntry(connector string) (bool, error) {
	path, err := w.pathFor(connector)
	if err != nil {
		return false, err
	}
	file, err := loadConnectorsFile(path)
	if err != nil {
		return false, err
	}
	_, ok := file.Connectors[connector]
	return ok, nil
}

// loadEntry returns the current on-disk entry for a connector.
func (w *connectorWriter) loadEntry(connector string) (ConnectorCreds, bool, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	path, err := w.pathFor(connector)
	if err != nil {
		return ConnectorCreds{}, false, err
	}
	file, err := loadConnectorsFile(path)
	if err != nil {
		return ConnectorCreds{}, false, err
	}
	entry, ok := file.Connectors[connector]
	if !ok {
		return ConnectorCreds{}, false, nil
	}
	return credsFromEntry(connector, entry), true, nil
}
