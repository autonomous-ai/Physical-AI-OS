package mqtthandler

import (
	"context"
	"path/filepath"
	"testing"

	"go.autonomous.ai/os/runtimes/openclaw"
)

// fakeMCPGateway records WriteMCPEntry/RemoveMCPEntry calls.
type fakeMCPGateway struct {
	written  map[string]map[string]any
	removed  []string
	writeErr error
}

func (f *fakeMCPGateway) WriteMCPEntry(name string, entry map[string]any) error {
	if f.writeErr != nil {
		return f.writeErr
	}
	if f.written == nil {
		f.written = map[string]map[string]any{}
	}
	f.written[name] = entry
	return nil
}

func (f *fakeMCPGateway) RemoveMCPEntry(name string) (bool, error) {
	f.removed = append(f.removed, name)
	return true, nil
}

// authHeaderOf digs the Authorization value out of a recorded mcp entry.
func authHeaderOf(t *testing.T, entry map[string]any) string {
	t.Helper()
	headers, ok := entry["headers"].(map[string]any)
	if !ok {
		t.Fatalf("entry has no headers map: %+v", entry)
	}
	auth, _ := headers["Authorization"].(string)
	return auth
}

// mustURL fetches a catalog URL for the fallback-routing assertions.
func mustURL(t *testing.T, code string) string {
	t.Helper()
	url, ok := openclaw.MCPConnectorURL(code)
	if !ok {
		t.Fatalf("openclaw.MCPConnectorURL(%q) not found", code)
	}
	return url
}

func TestConnectorWriter_PathConvention(t *testing.T) {
	dir := t.TempDir()
	w := newConnectorWriter(dir, &fakeMCPGateway{}, nil)
	got, err := w.pathFor("intercom")
	if err != nil {
		t.Fatalf("pathFor: %v", err)
	}
	if want := filepath.Join(dir, "intercom_access_tokens.json"); got != want {
		t.Fatalf("pathFor = %q, want %q", got, want)
	}
}

// Untrusted connector codes must not escape configsDir via path traversal.
func TestConnectorWriter_RejectsUnsafeCode(t *testing.T) {
	dir := t.TempDir()
	fake := &fakeMCPGateway{}
	w := newConnectorWriter(dir, fake, nil)
	ctx := context.Background()

	for _, bad := range []string{"../evil", "a/b", "../../root/.ssh/authorized_keys", "UPPER", "with space", ""} {
		if _, err := w.pathFor(bad); err == nil {
			t.Fatalf("pathFor(%q) accepted, want rejected", bad)
		}
		if err := w.Write(ctx, ConnectorCreds{Connector: bad, AuthType: "oauth", AccessToken: "at"}); err == nil {
			t.Fatalf("Write(%q) accepted, want rejected", bad)
		}
		if _, err := w.Remove(ctx, bad); err == nil {
			t.Fatalf("Remove(%q) accepted, want rejected", bad)
		}
	}
	if len(fake.written) != 0 {
		t.Fatalf("unsafe codes leaked mcp entries: %v", fake.written)
	}
}

// Payload-supplied mcp_url + mcp_auth_header drive routing with no fallback row.
func TestConnectorWriter_DataDrivenMCPRouting(t *testing.T) {
	dir := t.TempDir()
	fake := &fakeMCPGateway{}
	w := newConnectorWriter(dir, fake, nil)

	creds := ConnectorCreds{
		Connector: "intercom",
		AuthType:  "api_key",
		APIKey:    "ic-key",
		Credentials: map[string]string{
			credentialMCPURL:        "https://mcp.intercom.com/mcp",
			credentialMCPAuthHeader: authHeaderBearerAPIKey,
		},
	}
	if err := w.Write(context.Background(), creds); err != nil {
		t.Fatalf("Write: %v", err)
	}

	if _, ok, err := w.loadEntry("intercom"); err != nil || !ok {
		t.Fatalf("loadEntry intercom: ok=%v err=%v", ok, err)
	}
	entry, ok := fake.written["intercom"]
	if !ok {
		t.Fatalf("mcp entry not written for intercom; written=%v", fake.written)
	}
	if entry["url"] != "https://mcp.intercom.com/mcp" {
		t.Fatalf("url = %v, want intercom mcp url", entry["url"])
	}
	if got := authHeaderOf(t, entry); got != "Bearer ic-key" {
		t.Fatalf("Authorization = %q, want %q", got, "Bearer ic-key")
	}
}

// Payload mcp_url overrides the compiled-in fallback for a known code.
func TestConnectorWriter_PayloadOverridesFallback(t *testing.T) {
	dir := t.TempDir()
	fake := &fakeMCPGateway{}
	w := newConnectorWriter(dir, fake, nil)

	creds := ConnectorCreds{
		Connector:   "notion",
		AuthType:    "oauth",
		AccessToken: "at",
		Credentials: map[string]string{credentialMCPURL: "https://custom.example/mcp"},
	}
	if err := w.Write(context.Background(), creds); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if got := fake.written["notion"]["url"]; got != "https://custom.example/mcp" {
		t.Fatalf("url = %v, want payload override", got)
	}
}

// Known MCP connectors fall back to the table when the payload has no mcp_url.
func TestConnectorWriter_FallbackRouting(t *testing.T) {
	cases := []struct {
		connector string
		creds     ConnectorCreds
		wantURL   string
		wantAuth  string
	}{
		{
			connector: "notion",
			creds:     ConnectorCreds{Connector: "notion", AuthType: "oauth", AccessToken: "at"},
			wantURL:   mustURL(t, "notion"),
			wantAuth:  "Bearer at",
		},
		{
			connector: "ahrefs",
			creds:     ConnectorCreds{Connector: "ahrefs", AuthType: "api_key", APIKey: "ak"},
			wantURL:   mustURL(t, "ahrefs"),
			wantAuth:  "Bearer ak",
		},
	}
	for _, tc := range cases {
		t.Run(tc.connector, func(t *testing.T) {
			fake := &fakeMCPGateway{}
			w := newConnectorWriter(t.TempDir(), fake, nil)
			if err := w.Write(context.Background(), tc.creds); err != nil {
				t.Fatalf("Write: %v", err)
			}
			entry, ok := fake.written[tc.connector]
			if !ok {
				t.Fatalf("no mcp entry for %s", tc.connector)
			}
			if entry["url"] != tc.wantURL {
				t.Fatalf("url = %v, want %v", entry["url"], tc.wantURL)
			}
			if got := authHeaderOf(t, entry); got != tc.wantAuth {
				t.Fatalf("Authorization = %q, want %q", got, tc.wantAuth)
			}
		})
	}
}

// Credential-only connectors get a token file and no openclaw entry.
func TestConnectorWriter_CredentialOnlyNoMCPEntry(t *testing.T) {
	dir := t.TempDir()
	fake := &fakeMCPGateway{}
	w := newConnectorWriter(dir, fake, nil)

	creds := ConnectorCreds{Connector: "gmail", AuthType: "oauth", AccessToken: "at", RefreshToken: "rt"}
	if err := w.Write(context.Background(), creds); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if _, ok, _ := w.loadEntry("gmail"); !ok {
		t.Fatalf("gmail token file not written")
	}
	if len(fake.written) != 0 {
		t.Fatalf("expected no mcp entry for gmail, got %v", fake.written)
	}
}

// RefreshableEntries gates on refresh_token + refresh:true.
func TestConnectorWriter_RefreshableEntriesAcrossFiles(t *testing.T) {
	dir := t.TempDir()
	w := newConnectorWriter(dir, &fakeMCPGateway{}, nil)

	mustWrite(t, w, ConnectorCreds{Connector: "notion", AuthType: "oauth", AccessToken: "at", RefreshToken: "rt-n", Refresh: true, ExpiresAt: 111})
	mustWrite(t, w, ConnectorCreds{Connector: "gmail", AuthType: "oauth", AccessToken: "at", RefreshToken: "rt-g", Refresh: false})
	mustWrite(t, w, ConnectorCreds{Connector: "figma", AuthType: "oauth", AccessToken: "at", Refresh: true})

	got := w.RefreshableEntries()
	if len(got) != 1 {
		t.Fatalf("expected 1 eligible entry, got %d: %+v", len(got), got)
	}
	if got[0].Connector != "notion" || got[0].RefreshToken != "rt-n" || got[0].ExpiresAt != 111 {
		t.Fatalf("unexpected target: %+v", got[0])
	}
}

// Codes owned by a special writer are excluded from the generic refresh glob.
func TestConnectorWriter_RefreshableEntriesSkipsReserved(t *testing.T) {
	dir := t.TempDir()
	w := newConnectorWriter(dir, &fakeMCPGateway{}, map[string]bool{"figma-api": true})

	// figma-api token file is eligible on its face (refresh_token + refresh:true)
	// but is owned by a special writer → must be skipped here.
	mustWrite(t, w, ConnectorCreds{Connector: "figma-api", AuthType: "oauth", AccessToken: "at", RefreshToken: "rt-f", Refresh: true, ExpiresAt: 222})
	mustWrite(t, w, ConnectorCreds{Connector: "notion", AuthType: "oauth", AccessToken: "at", RefreshToken: "rt-n", Refresh: true, ExpiresAt: 111})

	got := w.RefreshableEntries()
	if len(got) != 1 || got[0].Connector != "notion" {
		t.Fatalf("expected only notion, got %+v", got)
	}
}

func TestConnectorWriter_Remove(t *testing.T) {
	dir := t.TempDir()
	fake := &fakeMCPGateway{}
	w := newConnectorWriter(dir, fake, nil)
	ctx := context.Background()

	removed, err := w.Remove(ctx, "notion")
	if err != nil || removed {
		t.Fatalf("Remove(absent) = removed=%v err=%v, want false,nil", removed, err)
	}

	mustWrite(t, w, ConnectorCreds{Connector: "notion", AuthType: "oauth", AccessToken: "at"})
	removed, err = w.Remove(ctx, "notion")
	if err != nil || !removed {
		t.Fatalf("Remove(present) = removed=%v err=%v, want true,nil", removed, err)
	}
	if _, ok, _ := w.loadEntry("notion"); ok {
		t.Fatalf("entry still present after Remove")
	}
	if len(fake.removed) == 0 || fake.removed[len(fake.removed)-1] != "notion" {
		t.Fatalf("RemoveMCPEntry not called for notion: %v", fake.removed)
	}
}

func TestConnectorWriter_ImplementsInterfaces(t *testing.T) {
	w := newConnectorWriter(t.TempDir(), &fakeMCPGateway{}, nil)
	var _ ConnectorWriter = w
	var _ entryLoader = w
}

func mustWrite(t *testing.T, w *connectorWriter, creds ConnectorCreds) {
	t.Helper()
	if err := w.Write(context.Background(), creds); err != nil {
		t.Fatalf("Write(%s): %v", creds.Connector, err)
	}
}

func TestConnectorAuthHeader(t *testing.T) {
	cases := []struct {
		name       string
		descriptor string
		creds      ConnectorCreds
		wantName   string
		wantValue  string
		wantToken  string
	}{
		{name: "default empty -> bearer access_token", descriptor: "", creds: ConnectorCreds{AccessToken: "at"}, wantName: "Authorization", wantValue: "Bearer at", wantToken: "at"},
		{name: "bearer_access_token", descriptor: authHeaderBearerAccessToken, creds: ConnectorCreds{AccessToken: "at"}, wantName: "Authorization", wantValue: "Bearer at", wantToken: "at"},
		{name: "bearer_api_key", descriptor: authHeaderBearerAPIKey, creds: ConnectorCreds{APIKey: "key"}, wantName: "Authorization", wantValue: "Bearer key", wantToken: "key"},
		{name: "custom header with pat -> api_key, no prefix", descriptor: "header:X-Figma-Token", creds: ConnectorCreds{AuthType: "pat", APIKey: "pat123"}, wantName: "X-Figma-Token", wantValue: "pat123", wantToken: "pat123"},
		{name: "custom header non-pat -> access_token, no prefix", descriptor: "header:X-Figma-Token", creds: ConnectorCreds{AuthType: "oauth", AccessToken: "oauthtok"}, wantName: "X-Figma-Token", wantValue: "oauthtok", wantToken: "oauthtok"},
		{name: "custom header with api_key auth_type -> api_key", descriptor: "header:X-Api-Key", creds: ConnectorCreds{AuthType: "api_key", APIKey: "k"}, wantName: "X-Api-Key", wantValue: "k", wantToken: "k"},
		{name: "custom header named Authorization still gets Bearer", descriptor: "header:Authorization", creds: ConnectorCreds{AuthType: "pat", APIKey: "pat123"}, wantName: "Authorization", wantValue: "Bearer pat123", wantToken: "pat123"},
		{name: "unknown descriptor falls back to bearer access_token", descriptor: "garbage", creds: ConnectorCreds{AccessToken: "at"}, wantName: "Authorization", wantValue: "Bearer at", wantToken: "at"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			name, value, token := connectorAuthHeader(tc.descriptor, tc.creds)
			if name != tc.wantName || value != tc.wantValue || token != tc.wantToken {
				t.Fatalf("connectorAuthHeader(%q) = (%q,%q,%q), want (%q,%q,%q)",
					tc.descriptor, name, value, token, tc.wantName, tc.wantValue, tc.wantToken)
			}
		})
	}
}

func TestConnectorWriter_CustomHeaderEntry(t *testing.T) {
	dir := t.TempDir()
	fake := &fakeMCPGateway{}
	w := newConnectorWriter(dir, fake, nil)

	creds := ConnectorCreds{
		Connector: "figmate", AuthType: "pat", APIKey: "pat123",
		Credentials: map[string]string{
			"mcp_url":         "https://example.com/mcp",
			"mcp_auth_header": "header:X-Figma-Token",
		},
	}
	if err := w.Write(context.Background(), creds); err != nil {
		t.Fatalf("Write: %v", err)
	}
	entry, ok := fake.written["figmate"]
	if !ok {
		t.Fatalf("no entry written for figmate: %v", fake.written)
	}
	headers, _ := entry["headers"].(map[string]any)
	if headers == nil {
		t.Fatalf("no headers in entry: %+v", entry)
	}
	if _, hasAuth := headers["Authorization"]; hasAuth {
		t.Fatalf("expected no Authorization header, got %+v", headers)
	}
	if got := headers["X-Figma-Token"]; got != "pat123" {
		t.Fatalf("X-Figma-Token = %v, want pat123", got)
	}
}
