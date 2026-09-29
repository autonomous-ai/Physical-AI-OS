package picoclaw

import (
	"encoding/json"
	"testing"
)

// mcpServers digs tools.mcp.servers out of a decoded config, failing the test when
// any level is missing or the wrong type.
func mcpServers(t *testing.T, cfg map[string]any) map[string]any {
	t.Helper()
	tools, ok := cfg["tools"].(map[string]any)
	if !ok {
		t.Fatalf("tools missing or wrong type: %#v", cfg["tools"])
	}
	mcp, ok := tools["mcp"].(map[string]any)
	if !ok {
		t.Fatalf("tools.mcp missing or wrong type: %#v", tools["mcp"])
	}
	servers, ok := mcp["servers"].(map[string]any)
	if !ok {
		t.Fatalf("tools.mcp.servers missing or wrong type: %#v", mcp["servers"])
	}
	return servers
}

func TestApplyMCPServerWrite_NestsUnderToolsMCPAndEnablesGate(t *testing.T) {
	// Fresh config with no tools block at all — the common case on a picoclaw-only
	// device where presync never seeds tools.mcp.
	cfg := map[string]any{"channel_list": map[string]any{"pico": map[string]any{"enabled": true}}}

	applyMCPServerWrite(cfg, "notion", map[string]any{
		"type": "http",
		"url":  "https://mcp.notion.com/mcp",
		"headers": map[string]any{
			"Authorization": "Bearer tok",
		},
	})

	// Gate must be flipped on — an absent/false tools.mcp.enabled makes picoclaw
	// silently ignore every server.
	tools := cfg["tools"].(map[string]any)
	mcp := tools["mcp"].(map[string]any)
	if mcp["enabled"] != true {
		t.Fatalf("tools.mcp.enabled = %#v, want true", mcp["enabled"])
	}

	notion := mcpServers(t, cfg)["notion"].(map[string]any)
	if notion["enabled"] != true {
		t.Errorf("server enabled = %#v, want true", notion["enabled"])
	}
	// type must survive verbatim (guards picoclaw's empty-type→sse inference).
	if notion["type"] != "http" {
		t.Errorf("server type = %#v, want http", notion["type"])
	}
	if notion["url"] != "https://mcp.notion.com/mcp" {
		t.Errorf("server url = %#v", notion["url"])
	}

	if _, ok := cfg["channel_list"]; !ok {
		t.Error("channel_list was dropped")
	}
}

func TestApplyMCPServerWrite_StdioPassthrough(t *testing.T) {
	cfg := map[string]any{}
	applyMCPServerWrite(cfg, "figma-api", map[string]any{
		"command": "node",
		"args":    []any{"/root/.picoclaw/figma.js"},
		"env":     map[string]any{"FIGMA_TOKEN": "t"},
	})

	figma := mcpServers(t, cfg)["figma-api"].(map[string]any)
	if figma["command"] != "node" {
		t.Errorf("command = %#v, want node", figma["command"])
	}
	if figma["enabled"] != true {
		t.Errorf("enabled = %#v, want true", figma["enabled"])
	}
	if _, ok := figma["type"]; ok {
		t.Error("stdio entry should not carry a type key")
	}
}

func TestApplyMCPServerWrite_UpsertAndGateStaysOn(t *testing.T) {
	cfg := map[string]any{}
	applyMCPServerWrite(cfg, "notion", map[string]any{"type": "http", "url": "a"})
	applyMCPServerWrite(cfg, "linear", map[string]any{"type": "http", "url": "b"})
	applyMCPServerWrite(cfg, "notion", map[string]any{"type": "http", "url": "c"})

	servers := mcpServers(t, cfg)
	if len(servers) != 2 {
		t.Fatalf("server count = %d, want 2", len(servers))
	}
	if servers["notion"].(map[string]any)["url"] != "c" {
		t.Errorf("notion url = %#v, want c (overwritten)", servers["notion"].(map[string]any)["url"])
	}
}

func TestApplyMCPServerRemove(t *testing.T) {
	cfg := map[string]any{}
	applyMCPServerWrite(cfg, "notion", map[string]any{"type": "http", "url": "a"})
	applyMCPServerWrite(cfg, "linear", map[string]any{"type": "http", "url": "b"})

	if !applyMCPServerRemove(cfg, "notion") {
		t.Fatal("remove notion = false, want true")
	}
	servers := mcpServers(t, cfg)
	if _, ok := servers["notion"]; ok {
		t.Error("notion still present after remove")
	}
	if _, ok := servers["linear"]; !ok {
		t.Error("linear was wrongly removed")
	}
	if cfg["tools"].(map[string]any)["mcp"].(map[string]any)["enabled"] != true {
		t.Error("tools.mcp.enabled was cleared")
	}
}

func TestApplyMCPServerRemove_AbsentIsNoop(t *testing.T) {
	// Missing tools block entirely.
	if applyMCPServerRemove(map[string]any{}, "notion") {
		t.Error("remove on empty config returned true")
	}
	cfg := map[string]any{}
	applyMCPServerWrite(cfg, "linear", map[string]any{"type": "http", "url": "b"})
	if applyMCPServerRemove(cfg, "notion") {
		t.Error("remove of absent server returned true")
	}
}

// TestRoundTripConfigHelpers checks read→write survives a JSON round-trip and keeps
// the nested MCP shape (the on-disk format picoclaw actually reads).
func TestRoundTripConfigHelpers(t *testing.T) {
	dir := t.TempDir()
	path := dir + "/config.json"

	seed := map[string]any{"gateway": map[string]any{"port": 18790}}
	if err := writePicoclawConfig(path, seed); err != nil {
		t.Fatalf("write seed: %v", err)
	}

	cfg, err := readPicoclawConfig(path)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	applyMCPServerWrite(cfg, "notion", map[string]any{"type": "http", "url": "a"})
	if err := writePicoclawConfig(path, cfg); err != nil {
		t.Fatalf("write: %v", err)
	}

	reloaded, err := readPicoclawConfig(path)
	if err != nil {
		t.Fatalf("reload: %v", err)
	}
	if _, ok := mcpServers(t, reloaded)["notion"]; !ok {
		t.Error("notion missing after round-trip")
	}
	raw, _ := json.Marshal(reloaded)
	var probe struct {
		Tools struct {
			MCP struct {
				Enabled bool                      `json:"enabled"`
				Servers map[string]map[string]any `json:"servers"`
			} `json:"mcp"`
		} `json:"tools"`
	}
	if err := json.Unmarshal(raw, &probe); err != nil {
		t.Fatalf("probe unmarshal: %v", err)
	}
	if !probe.Tools.MCP.Enabled {
		t.Error("tools.mcp.enabled not true in serialized form")
	}
	if probe.Tools.MCP.Servers["notion"]["url"] != "a" {
		t.Errorf("serialized notion url = %#v", probe.Tools.MCP.Servers["notion"]["url"])
	}
}
