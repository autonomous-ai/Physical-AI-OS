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

// validConnectorCode bounds a connector code to a safe charset before it is used
// as a filename component (pathFor) or an mcp.servers.<code> key. The code comes
// from the connector.set.<code> wire payload — untrusted input — so anything
// outside [a-z0-9_-] (e.g. "/" or ".." for path traversal) is rejected. Every
// real connector code (notion, google_calendar, figma-api, …) matches; note the
// charset excludes "/" and "." so no traversal sequence can be formed.
var validConnectorCode = regexp.MustCompile(`^[a-z0-9_-]{1,64}$`)

// Credential-map keys the backend sets in the connector's connector-auth `extra`.
// They flow verbatim into ConnectorCreds.Credentials and tell the device how to
// wire the connector — replacing the former compile-time registry rows.
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

// mcpEntryWriter is the subset of the agent gateway the connector writer needs.
// domain.AgentGateway (backed by openclaw.OpenclawService) satisfies it; tests supply a
// fake to assert routing without touching openclaw.json / restarting the gateway.
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
// connector. It replaces the former per-code registry (default + mcp + oauth
// writers). Persistence and the optional mcp.servers.<code> side-effect are
// decided per-message from ConnectorCreds.Credentials, with a compiled-in
// fallback table for the connectors that shipped before the contract moved to
// the wire. Safe for concurrent calls — one mutex guards all per-connector
// files (low volume; the refresh loop and handler rarely overlap).
type connectorWriter struct {
	mu      sync.Mutex
	dir     string
	gateway mcpEntryWriter
	// fallback maps connector code → routing for the connectors implemented
	// before mcp_url/mcp_auth_header were carried on the wire. Payload values
	// always win; this only fills the gap until the dashboard `extra` is set.
	fallback map[string]mcpRouting
	// reserved is the set of connector codes owned by a special writer
	// (handler.specialConnectorWriters). RefreshableEntries skips their token
	// files so the generic writer never re-Writes a connector it doesn't own
	// (which would clobber, e.g., figma-api's stdio entry with an http one).
	reserved map[string]bool
}

// newConnectorWriter builds the writer. configsDir is typically
// `<OpenclawConfigDir>/workspace/configs`. The fallback table is sourced from
// the openclaw catalog (the single source of truth for those URLs) via the
// mcpConnectorSpecs list. reserved lists codes handled by a special writer
// (may be nil).
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

// pathFor is the per-connector token file. Same `<code>_access_tokens.json`
// convention every former writer used, so existing on-disk files are unchanged.
// Rejects codes outside the safe charset so an untrusted code can't escape
// configsDir via path traversal.
func (w *connectorWriter) pathFor(connector string) (string, error) {
	if !validConnectorCode.MatchString(connector) {
		return "", fmt.Errorf("invalid connector code %q", connector)
	}
	return filepath.Join(w.dir, connector+"_access_tokens.json"), nil
}

// resolveRouting decides whether this connector is an MCP server and how to
// build its Authorization header. Payload (credentials) wins; otherwise the
// fallback table; otherwise empty url → credential-only connector.
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
// header for the mcp.servers entry, from the descriptor + creds. Returns the
// header name, the full header value (Bearer-prefixed for Authorization, raw
// otherwise), and the raw token (for stdio writers that build their own header).
func connectorAuthHeader(descriptor string, creds ConnectorCreds) (name, value, token string) {
	switch {
	case descriptor == authHeaderBearerAPIKey:
		return "Authorization", "Bearer " + creds.APIKey, creds.APIKey
	case strings.HasPrefix(descriptor, authHeaderCustomPrefix):
		hdr := strings.TrimSpace(strings.TrimPrefix(descriptor, authHeaderCustomPrefix))
		// Prefer the pasted api_key (PAT / static key); fall back to access_token
		// (OAuth). Whichever field is populated is the credential — this avoids
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
// server, upserts mcp.servers.<code> in openclaw.json. A token file is always
// written; the MCP entry is conditional. If the MCP step fails the credentials
// are still on disk — the refresh loop / a later connector.set retries the
// openclaw side without re-fetching tokens.
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
		// Credential-only connector (e.g. gmail/google_*): no openclaw entry.
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

// Remove deletes the token-file entry and the openclaw.json MCP entry (if any).
// RemoveMCPEntry is idempotent — a no-op (no restart) when the connector never
// had an entry, so calling it unconditionally is safe for credential-only codes.
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
// refresh loop should rotate. Universal rule (BE owns eligibility): a
// refresh_token AND refresh:true. The connector code is read from each file's
// map key, not parsed from the filename. The legacy bare access_tokens.json
// does not match the `*_access_tokens.json` glob.
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

// hasEntry reports whether connector's token file holds an entry for it —
// the "is it installed?" question the scheduled-task connector guard asks
// (see connectorInstalled).
//
// Deliberately LOCK-FREE, unlike every other method here: Write holds w.mu
// across WriteMCPEntry, which restarts the openclaw gateway (30-60s on a Pi),
// and this is asked from the schedule runner's tick and from the schedule.run
// MQTT handler, neither of which may stall for that long. It is safe without
// the lock because writeConnectorsFile replaces the file by tmp+rename, so a
// reader always sees a complete old or new file, and w.dir never changes after
// construction.
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

// loadEntry returns the current on-disk entry for a connector. Satisfies
// entryLoader so the refresh loop preserves fields the BE refresh response does
// not re-send (scopes, client_id, and the credentials map — which now carries
// mcp_url/mcp_auth_header, letting a rotation rebuild the openclaw entry).
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
