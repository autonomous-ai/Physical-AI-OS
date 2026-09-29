package gatewayd

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestSteerPrioritizesUserRequestAndPreservesResponseRoute(t *testing.T) {
	content := "[user] Ask Codex to review the project.\n[harness-reply run_id=device-chat-30 channel=voice]"
	input := appSteerInput(turnPayload{Content: content})
	text, _ := input[0]["text"].(string)
	if !strings.HasSuffix(text, content) || !strings.Contains(text, "Prioritize this request") {
		t.Fatalf("steer lost the user request or its priority: %q", text)
	}
	sync := "[voice_agent_handled] The realtime agent already answered."
	input = appSteerInput(turnPayload{Content: sync})
	if input[0]["text"] != sync {
		t.Fatalf("silent history sync was promoted to a user request: %v", input)
	}
}

func TestGatewaySteersConcurrentMessagesIntoActiveTurn(t *testing.T) {
	dir := t.TempDir()
	binary := writeFakeCodex(t, dir, filepath.Join(dir, "argv.log"))
	url, _ := startServer(t, binary, dir)
	conn := dial(t, url, testToken)
	_ = readFrame(t, conn)
	for i := 1; i <= 3; i++ {
		if err := conn.WriteJSON(map[string]any{"type": "message.send", "id": fmt.Sprintf("req-%d", i), "run_id": fmt.Sprintf("run-%d", i), "payload": map[string]any{"content": fmt.Sprintf("followup-%d", i)}}); err != nil {
			t.Fatal(err)
		}
	}
	frames := readTurnFrames(t, conn)
	if len(frames) < 3 {
		t.Fatalf("active turn missing events: %v", frames)
	}
	for _, frame := range frames {
		if frame["type"] == "bridge.steered" {
			continue
		}
		if frame["request_id"] != "req-1" || frame["run_id"] != "run-1" {
			t.Fatalf("steered turn must retain first correlation: %v", frame)
		}
	}
	steered := 0
	for _, frame := range frames {
		if frame["type"] == "bridge.steered" && frame["request_id"] == "req-2" && frame["run_id"] == "run-2" {
			steered++
		}
		if frame["type"] == "bridge.steered" && frame["request_id"] == "req-3" && frame["run_id"] == "run-3" {
			steered++
		}
	}
	if steered != 2 {
		t.Fatalf("want acknowledgements for both steered requests, got %d frames: %v", steered, frames)
	}
	data, err := os.ReadFile(filepath.Join(dir, "argv.log"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Count(string(data), "METHOD:turn/steer") != 2 {
		t.Fatalf("want two turn/steer requests, log: %s", data)
	}
}

func TestGatewayInterruptsTimedOutAppServerTurn(t *testing.T) {
	dir := t.TempDir()
	binary := writeFakeCodex(t, dir, filepath.Join(dir, "argv.log"))
	url, _ := startServerTimeout(t, binary, dir, 25*time.Millisecond)
	conn := dial(t, url, testToken)
	_ = readFrame(t, conn)
	sendMessage(t, conn, "slow turn")
	frames := readTurnFrames(t, conn)
	last := frames[len(frames)-1]
	if last["type"] != "bridge.error" || last["error"] != "timeout" {
		t.Fatalf("want timeout bridge error, got %v", last)
	}
	data, err := os.ReadFile(filepath.Join(dir, "argv.log"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), "METHOD:turn/interrupt") {
		t.Fatalf("timeout did not interrupt active App Server turn: %s", data)
	}
}

func TestCorrelationNeverTagsControlOrRejectionFrames(t *testing.T) {
	for _, kind := range []string{"pong", "bridge.status", "bridge.rejected"} {
		raw, _ := json.Marshal(map[string]any{"type": kind, "request_id": "other", "run_id": "run-other"})
		var result map[string]any
		_ = json.Unmarshal(correlateTurnFrame(raw, "active", "run-active"), &result)
		if result["request_id"] != "other" || result["run_id"] != "run-other" {
			t.Fatalf("control frame mislabeled: %v", result)
		}
	}
}
