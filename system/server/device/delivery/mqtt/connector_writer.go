package mqtthandler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"time"

	"go.autonomous.ai/os/system/domain"
)

const (
	connectorsSchemaVer = 1
	connectorsFileMode  = 0o600
	connectorsDirMode   = 0o700
)

// ConnectorCreds is the in-process, post-validation representation of a
// connector.set.<code> payload.
type ConnectorCreds struct {
	Connector    string
	AuthType     string
	AccessToken  string
	RefreshToken string
	TokenType    string
	ExpiresAt    int64 // absolute unix seconds; 0 if BE didn't send expires_in
	APIKey       string
	Scopes       []string
	UserEmail    string
	ClientID     string
	Credentials  map[string]string
	Refresh      bool  // refresh-eligibility flag from connector.set; gates the refresh loop
	ObtainedAt   int64 // unix seconds when this device received the credentials
}

// ConnectorRefreshTarget is one entry the refresh loop can act on.
type ConnectorRefreshTarget struct {
	Connector    string
	RefreshToken string
	ExpiresAt    int64
}

// ConnectorWriter owns persistence (and any side-effects like writing an MCP
// entry into openclaw.json) for connector credentials. Implementations must be
// safe for concurrent calls.
type ConnectorWriter interface {
	// Write persists the credentials. Replaces any existing entry for the
	// same connector. Idempotent on identical input.
	Write(ctx context.Context, creds ConnectorCreds) error

	// Remove deletes the persisted entry. Returns removed=false when the
	// entry was already absent (no-op, no error).
	Remove(ctx context.Context, connector string) (removed bool, err error)

	// RefreshableEntries returns the subset of stored entries that have a
	// refresh_token AND refresh:true.
	RefreshableEntries() []ConnectorRefreshTarget
}

// loadConnectorsFile reads a ConnectorsFile from disk.
func loadConnectorsFile(path string) (domain.ConnectorsFile, error) {
	out := domain.ConnectorsFile{Version: connectorsSchemaVer, Connectors: map[string]domain.ConnectorEntry{}}
	data, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return out, nil
		}
		return out, fmt.Errorf("read %s: %w", path, err)
	}
	if len(data) == 0 {
		return out, nil
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return out, fmt.Errorf("parse %s: %w", path, err)
	}
	if out.Connectors == nil {
		out.Connectors = map[string]domain.ConnectorEntry{}
	}
	if out.Version == 0 {
		out.Version = connectorsSchemaVer
	}
	return out, nil
}

// writeConnectorsFile persists via tmp+rename so a mid-write crash cannot
// leave a truncated file behind. Mode 0600 — credentials are inside.
func writeConnectorsFile(path string, file domain.ConnectorsFile) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, connectorsDirMode); err != nil {
		return fmt.Errorf("mkdir %s: %w", dir, err)
	}
	data, err := json.MarshalIndent(file, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal: %w", err)
	}
	tmp, err := os.CreateTemp(dir, ".connectors.*.tmp")
	if err != nil {
		return fmt.Errorf("create tmp: %w", err)
	}
	tmpPath := tmp.Name()
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return fmt.Errorf("write tmp: %w", err)
	}
	if err := tmp.Chmod(connectorsFileMode); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return fmt.Errorf("chmod tmp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("close tmp: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		os.Remove(tmpPath)
		return fmt.Errorf("rename tmp: %w", err)
	}
	return nil
}

// connectorEntryFromCreds is the in-memory → on-disk projection. Centralised
// so default and per-connector writers can't drift on field naming.
func connectorEntryFromCreds(c ConnectorCreds) domain.ConnectorEntry {
	obtained := c.ObtainedAt
	if obtained == 0 {
		obtained = time.Now().Unix()
	}
	return domain.ConnectorEntry{
		AuthType:     c.AuthType,
		AccessToken:  c.AccessToken,
		RefreshToken: c.RefreshToken,
		TokenType:    c.TokenType,
		ExpiresAt:    c.ExpiresAt,
		APIKey:       c.APIKey,
		Scopes:       c.Scopes,
		UserEmail:    c.UserEmail,
		ClientID:     c.ClientID,
		Credentials:  c.Credentials,
		Refresh:      c.Refresh,
		ObtainedAt:   obtained,
	}
}

// credsFromEntry is the inverse mapping — used by the refresh loop after
// rotating tokens to rebuild a ConnectorCreds for re-Write().
func credsFromEntry(connector string, entry domain.ConnectorEntry) ConnectorCreds {
	return ConnectorCreds{
		Connector:    connector,
		AuthType:     entry.AuthType,
		AccessToken:  entry.AccessToken,
		RefreshToken: entry.RefreshToken,
		TokenType:    entry.TokenType,
		ExpiresAt:    entry.ExpiresAt,
		APIKey:       entry.APIKey,
		Scopes:       entry.Scopes,
		UserEmail:    entry.UserEmail,
		ClientID:     entry.ClientID,
		Credentials:  entry.Credentials,
		Refresh:      entry.Refresh,
		ObtainedAt:   entry.ObtainedAt,
	}
}
