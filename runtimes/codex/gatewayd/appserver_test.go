package gatewayd

import (
	"path/filepath"
	"testing"
)

func TestAppInputIncludesTextAndImageDataURLs(t *testing.T) {
	p := turnPayload{Content: "look at this"}
	p.Attachments = append(p.Attachments,
		struct {
			Type string `json:"type"`
			URL  string `json:"url"`
		}{Type: "image", URL: "data:image/png;base64,AA=="},
		struct {
			Type string `json:"type"`
			URL  string `json:"url"`
		}{Type: "image", URL: "https://example.test/image.png"},
	)
	in := appInput(p)
	if len(in) != 2 || in[0]["type"] != "text" || in[0]["text"] != "look at this" {
		t.Fatalf("unexpected app input: %#v", in)
	}
	if in[1]["type"] != "image" || in[1]["url"] != "data:image/png;base64,AA==" {
		t.Fatalf("unexpected image input: %#v", in[1])
	}
}

func TestLegacyItemUsesExecTranslatorItemKinds(t *testing.T) {
	item := legacyItem([]byte(`{"id":"m1","type":"agentMessage","text":"hello"}`))
	if item["item_type"] != "agent_message" {
		t.Fatalf("item kind = %#v, want agent_message", item)
	}
}

func TestSnakeItemType(t *testing.T) {
	for got, want := range map[string]string{
		snakeItemType("commandExecution"): "command_execution",
		snakeItemType("mcpToolCall"):      "mcp_tool_call",
	} {
		if got != want {
			t.Fatalf("snake item type = %q, want %q", got, want)
		}
	}
}

func TestMissingAppThread(t *testing.T) {
	for _, raw := range []string{
		`{"message":"thread not found: stale-thread"}`,
		`{"message":"no rollout found for thread id stale-thread"}`,
		`{"message":"conversation not found"}`,
	} {
		if !missingAppThread([]byte(raw)) {
			t.Fatalf("missingAppThread(%s) = false", raw)
		}
	}
	if missingAppThread([]byte(`{"message":"rate limit exceeded"}`)) {
		t.Fatal("a non-session error must not retry on a fresh thread")
	}
}

// The App Server reports token usage on turn/completed.
func TestAppTurnCompletedForwardsUsage(t *testing.T) {
	dir := t.TempDir()
	url, _ := startServer(t, writeFakeCodex(t, dir, filepath.Join(dir, "argv.txt")), dir)
	conn := dial(t, url, testToken)
	readFrame(t, conn)

	sendMessage(t, conn, "hi codex")
	var completed map[string]any
	for i := 0; i < 8 && completed == nil; i++ {
		if f := readFrame(t, conn); f["type"] == "turn.completed" {
			completed = f
		}
	}
	if completed == nil {
		t.Fatal("no turn.completed frame")
	}
	usage, ok := completed["usage"].(map[string]any)
	if !ok {
		t.Fatalf("turn.completed carries no usage: %v", completed)
	}
	for field, want := range map[string]float64{
		"input_tokens": 723, "cached_input_tokens": 30720, "output_tokens": 31,
	} {
		if got, _ := usage[field].(float64); got != want {
			t.Fatalf("usage[%s] = %v, want %v", field, usage[field], want)
		}
	}
}

func TestUsageOfPrefersTheFirstNonEmptyBlock(t *testing.T) {
	if u := usageOf(nil, []byte(`null`), []byte(`{}`), []byte(`{"input_tokens":7}`)); u == nil || u["input_tokens"] != float64(7) {
		t.Fatalf("usageOf skipped past the populated block: %#v", u)
	}
	if u := usageOf(nil, []byte(`null`)); u != nil {
		t.Fatalf("usageOf with no real block = %#v, want nil", u)
	}
}
