package agent

import (
	"os"
	"path/filepath"
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// fakeMCPGateway records WriteMCPEntry calls.
type fakeMCPGateway struct {
	domain.AgentGateway
	written  map[string]map[string]any
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

func writeJSON(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestMCPReconcileNoOpWhenRuntimeUnchanged(t *testing.T) {
	gw := &fakeMCPGateway{}
	cfg := &config.Config{AgentRuntime: "hermes", MCPAppliedRuntime: "hermes"}
	(&MCPReconcile{cfg: cfg, gw: gw}).Reconcile()

	if len(gw.written) != 0 {
		t.Errorf("cloned %d entries with no runtime change, want 0", len(gw.written))
	}
}

func TestMCPReconcileFirstObservationRecordsBaseline(t *testing.T) {
	t.Chdir(t.TempDir()) // WithLockSave writes config/config.json relative to cwd

	gw := &fakeMCPGateway{}
	cfg := &config.Config{AgentRuntime: "hermes", MCPAppliedRuntime: ""}
	(&MCPReconcile{cfg: cfg, gw: gw}).Reconcile()

	if len(gw.written) != 0 {
		t.Errorf("first observation cloned %d entries, want 0 (baseline only)", len(gw.written))
	}
	if cfg.MCPAppliedRuntime != "hermes" {
		t.Errorf("baseline marker = %q, want hermes", cfg.MCPAppliedRuntime)
	}
}

func TestMCPReconcileClonesOpenclawToNewRuntime(t *testing.T) {
	t.Chdir(t.TempDir())

	ocDir := t.TempDir()
	writeJSON(t, filepath.Join(ocDir, "openclaw.json"), `{
	  "mcp": {"servers": {
	    "notion": {"type":"http","url":"https://mcp.notion.com/mcp","headers":{"Authorization":"Bearer tok"}},
	    "figma":  {"command":"node","args":["wrapper.js"]}
	  }}
	}`)

	gw := &fakeMCPGateway{}
	cfg := &config.Config{
		AgentRuntime:      "hermes",
		MCPAppliedRuntime: "openclaw",
		OpenclawConfigDir: ocDir,
	}
	(&MCPReconcile{cfg: cfg, gw: gw}).Reconcile()

	if len(gw.written) != 2 {
		t.Fatalf("cloned %d entries, want 2: %+v", len(gw.written), gw.written)
	}
	notion := gw.written["notion"]
	if notion["type"] != "http" || notion["url"] != "https://mcp.notion.com/mcp" {
		t.Errorf("notion entry not canonical: %+v", notion)
	}
	if gw.written["figma"]["command"] != "node" {
		t.Errorf("figma stdio entry not preserved: %+v", gw.written["figma"])
	}
	if cfg.MCPAppliedRuntime != "hermes" {
		t.Errorf("marker = %q, want advanced to hermes", cfg.MCPAppliedRuntime)
	}
}

func TestMCPReconcileCloneErrorLeavesMarker(t *testing.T) {
	t.Chdir(t.TempDir())

	ocDir := t.TempDir()
	writeJSON(t, filepath.Join(ocDir, "openclaw.json"),
		`{"mcp":{"servers":{"notion":{"type":"http","url":"u","headers":{}}}}}`)

	gw := &fakeMCPGateway{writeErr: os.ErrPermission}
	cfg := &config.Config{AgentRuntime: "hermes", MCPAppliedRuntime: "openclaw", OpenclawConfigDir: ocDir}
	(&MCPReconcile{cfg: cfg, gw: gw}).Reconcile()

	if cfg.MCPAppliedRuntime != "openclaw" {
		t.Errorf("marker advanced despite clone error (= %q), want left at openclaw for retry", cfg.MCPAppliedRuntime)
	}
}

func TestMCPReconcileNoEntriesAdvancesMarker(t *testing.T) {
	t.Chdir(t.TempDir())

	ocDir := t.TempDir()
	gw := &fakeMCPGateway{}
	cfg := &config.Config{AgentRuntime: "hermes", MCPAppliedRuntime: "openclaw", OpenclawConfigDir: ocDir}
	(&MCPReconcile{cfg: cfg, gw: gw}).Reconcile()

	if len(gw.written) != 0 {
		t.Errorf("cloned %d entries from empty config, want 0", len(gw.written))
	}
	if cfg.MCPAppliedRuntime != "hermes" {
		t.Errorf("marker = %q, want advanced (nothing to clone is still a clean pass)", cfg.MCPAppliedRuntime)
	}
}

func TestReadHermesMCP_NormalizesToCanonical(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(`
model:
  provider: custom:autonomous
mcp_servers:
  notion:
    url: https://mcp.notion.com/mcp
    headers:
      Authorization: Bearer tok
    enabled: true
  figma:
    command: node
    args: [wrapper.js]
    enabled: true
`), 0o644); err != nil {
		t.Fatal(err)
	}

	entries, err := readHermesMCP(path)
	if err != nil {
		t.Fatalf("readHermesMCP: %v", err)
	}
	notion := entries["notion"]
	if notion["type"] != "http" {
		t.Errorf("url-bearing entry should get type:http for openclaw, got %+v", notion)
	}
	if _, ok := notion["enabled"]; ok {
		t.Errorf("hermes-only enabled flag should be stripped, got %+v", notion)
	}
	if notion["url"] != "https://mcp.notion.com/mcp" {
		t.Errorf("url lost: %+v", notion)
	}
	figma := entries["figma"]
	if _, ok := figma["type"]; ok {
		t.Errorf("stdio entry should not get a type, got %+v", figma)
	}
	if figma["command"] != "node" {
		t.Errorf("stdio command lost: %+v", figma)
	}
}

func TestReadMCPEntries_UnknownRuntimeAndMissingFile(t *testing.T) {
	if e, err := readMCPEntries("picoclaw", &config.Config{}); err != nil || len(e) != 0 {
		t.Errorf("picoclaw: got (%v, %v), want (empty, nil)", e, err)
	}
	cfg := &config.Config{OpenclawConfigDir: t.TempDir()}
	if e, err := readMCPEntries(domain.AgentRuntimeOpenClaw, cfg); err != nil || len(e) != 0 {
		t.Errorf("missing openclaw.json: got (%v, %v), want (empty, nil)", e, err)
	}
}
