package gatewayd

import (
	"encoding/json"
	"fmt"
	"path/filepath"
	"testing"
)

func TestGatewayCorrelatesThreeQueuedTurns(t *testing.T) {
	dir := t.TempDir()
	binary := writeFakeOpenCode(t, dir, filepath.Join(dir, "argv.log"))
	url, _ := startServer(t, binary, dir)
	conn := dial(t, url, testToken)
	_ = readFrame(t, conn)
	for i := 1; i <= 3; i++ {
		if err := conn.WriteJSON(map[string]any{"type": "message.send", "id": fmt.Sprintf("req-%d", i), "run_id": fmt.Sprintf("run-%d", i), "payload": map[string]any{"content": fmt.Sprintf("followup-%d", i)}}); err != nil {
			t.Fatal(err)
		}
	}
	for i := 1; i <= 3; i++ {
		frames := readTurnFrames(t, conn)
		if len(frames) < 3 {
			t.Fatalf("turn %d missing events", i)
		}
		for _, frame := range frames {
			if frame["request_id"] != fmt.Sprintf("req-%d", i) || frame["run_id"] != fmt.Sprintf("run-%d", i) {
				t.Fatalf("turn %d misattributed: %v", i, frame)
			}
		}
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
