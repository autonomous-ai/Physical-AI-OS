package hermes

import (
	"path/filepath"
	"reflect"
	"testing"
)

func TestToHermesMCPEntry_HTTPDropsTypeAddsEnabled(t *testing.T) {
	in := map[string]any{
		"type": "http",
		"url":  "https://mcp.notion.com/mcp",
		"headers": map[string]any{
			"Authorization": "Bearer tok",
		},
	}
	got := toHermesMCPEntry(in)

	if _, ok := got["type"]; ok {
		t.Errorf("type should be dropped for hermes, got %+v", got)
	}
	if got["enabled"] != true {
		t.Errorf("enabled = %v, want true", got["enabled"])
	}
	if got["url"] != "https://mcp.notion.com/mcp" {
		t.Errorf("url not preserved: %+v", got)
	}
	if h, ok := got["headers"].(map[string]any); !ok || h["Authorization"] != "Bearer tok" {
		t.Errorf("headers not preserved: %+v", got)
	}
	// Input must not be mutated (the caller may reuse it).
	if _, ok := in["enabled"]; ok {
		t.Errorf("toHermesMCPEntry mutated the input map: %+v", in)
	}
}

func TestToHermesMCPEntry_StdioPreservesCommand(t *testing.T) {
	in := map[string]any{
		"command": "npx",
		"args":    []any{"-y", "pkg"},
		"env":     map[string]any{"K": "v"},
	}
	got := toHermesMCPEntry(in)

	if got["command"] != "npx" {
		t.Errorf("command not preserved: %+v", got)
	}
	if got["enabled"] != true {
		t.Errorf("enabled = %v, want true", got["enabled"])
	}
	if !reflect.DeepEqual(got["args"], []any{"-y", "pkg"}) {
		t.Errorf("args not preserved: %+v", got)
	}
}

// TestHermesConfigRoundTrip checks config.yaml round-trips nested mcp_servers and ensureYAMLMap upserts in place.
func TestHermesConfigRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.yaml")

	cfg := map[string]any{
		"model": map[string]any{"provider": "custom:autonomous"},
	}
	servers := ensureYAMLMap(cfg, "mcp_servers")
	servers["notion"] = toHermesMCPEntry(map[string]any{
		"type": "http",
		"url":  "https://mcp.notion.com/mcp",
		"headers": map[string]any{
			"Authorization": "Bearer tok",
		},
	})
	cfg["mcp_servers"] = servers

	if err := writeHermesConfig(path, cfg); err != nil {
		t.Fatalf("writeHermesConfig: %v", err)
	}

	back, err := readHermesConfig(path)
	if err != nil {
		t.Fatalf("readHermesConfig: %v", err)
	}
	gotServers, ok := back["mcp_servers"].(map[string]any)
	if !ok {
		t.Fatalf("mcp_servers not map[string]any: %T", back["mcp_servers"])
	}
	notion, ok := gotServers["notion"].(map[string]any)
	if !ok {
		t.Fatalf("notion not map[string]any: %T", gotServers["notion"])
	}
	if notion["url"] != "https://mcp.notion.com/mcp" {
		t.Errorf("url lost in round-trip: %+v", notion)
	}
	if notion["enabled"] != true {
		t.Errorf("enabled lost in round-trip: %+v", notion)
	}
	if _, ok := back["model"].(map[string]any); !ok {
		t.Errorf("model section dropped: %+v", back)
	}
}

func TestReadHermesConfig_NotExistErrors(t *testing.T) {
	if _, err := readHermesConfig(filepath.Join(t.TempDir(), "nope.yaml")); err == nil {
		t.Errorf("readHermesConfig on missing file should error (connectors are post-onboarding)")
	}
}
