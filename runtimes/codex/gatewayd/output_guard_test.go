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

func TestDegenerateAssistantOutput(t *testing.T) {
	var code strings.Builder
	for i := 0; i < 6000; i++ {
		fmt.Fprintf(&code, "func compute%d(input int) int { return input + %d } // distinct generated function\n", i, i)
	}
	cases := []struct {
		name string
		text string
		want bool
	}{
		{"observed accented syllables", "Am deschis pagina pentru a verifica disponibilitatea. " + strings.Repeat("că că căă căă că căă căă căä căä ", 16000), true},
		{"short repetition", strings.Repeat("că ", 1000), false},
		{"long code", code.String(), false},
		{"large numeric fixture", strings.Repeat("[0, 1, 2, 3, 4, 5, 6, 7],\n", 10000), false},
		{"long structured repetitive code", strings.Repeat("if (value != null) { return value; }\n", 10000), false},
		{"long explanation", strings.Repeat("This function accepts the current value and checks whether each requested operation satisfies its documented preconditions before returning the computed result.\n", 3000), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := degenerateAssistantOutput(tc.text); got != tc.want {
				t.Fatalf("classification = %v, want %v (bytes=%d)", got, tc.want, len(tc.text))
			}
		})
	}
}

func TestRejectedOutputQuarantinesThreadBeforeQueuedTurnWithoutReplay(t *testing.T) {
	t.Skip("legacy codex exec output quarantine test retired; App Server uses item notifications")
	for _, resumed := range []bool{false, true} {
		t.Run(fmt.Sprintf("resumed=%v", resumed), func(t *testing.T) {
			dir := t.TempDir()
			argvPath := filepath.Join(dir, "argv.log")
			counterPath := filepath.Join(dir, "counter")
			badPath := filepath.Join(dir, "bad.jsonl")
			bad, err := json.Marshal(map[string]any{"type": "item.completed", "item": map[string]any{
				"type": "agent_message", "text": strings.Repeat("că căă căä ", 40000),
			}})
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(badPath, append(bad, '\n'), 0600); err != nil {
				t.Fatal(err)
			}
			if resumed {
				if err := os.WriteFile(filepath.Join(dir, "session.json"), []byte(`{"thread_id":"existing-thread"}`), 0600); err != nil {
					t.Fatal(err)
				}
			}
			script := fmt.Sprintf(`#!/bin/bash
echo "ARGV:$*" >> %q
if [ ! -f %q ]; then
  touch %q
  echo '{"type":"thread.started","thread_id":"bad-thread"}'
  cat %q
  sleep 30
  echo '{"type":"turn.completed"}'
else
  cat <<'JSONL'
%s
JSONL
fi
`, argvPath, counterPath, counterPath, badPath, successJSONL)
			binary := writeScript(t, dir, "output-codex", script)
			url, cfg := startServer(t, binary, dir)
			conn := dial(t, url, testToken)
			_ = readFrame(t, conn)
			start := time.Now()
			sendMessage(t, conn, "first-task")
			sendMessage(t, conn, "next-task")
			rejected, completed := false, false
			for !completed {
				frame := readFrame(t, conn)
				switch frame["type"] {
				case "item.completed":
					item, _ := frame["item"].(map[string]any)
					if item["text"] != "hello" || !rejected {
						t.Fatal("rejected assistant item leaked before error")
					}
				case "bridge.error":
					if rejected || frame["error"] != degenerateOutputError {
						t.Fatalf("unexpected terminal error: %v", frame["error"])
					}
					rejected = true
				case "turn.completed":
					if !rejected {
						t.Fatal("rejected turn emitted success")
					}
					completed = true
				}
			}
			if elapsed := time.Since(start); elapsed > 5*time.Second {
				t.Fatalf("guard failed to terminate child promptly: %s", elapsed)
			}
			argv, err := os.ReadFile(argvPath)
			if err != nil {
				t.Fatal(err)
			}
			lines := strings.Split(strings.TrimSpace(string(argv)), "\n")
			if len(lines) != 2 || !strings.Contains(lines[0], "first-task") || !strings.Contains(lines[1], "next-task") {
				t.Fatalf("task was replayed or queued task lost: %q", lines)
			}
			if strings.Contains(lines[1], " resume ") {
				t.Fatalf("queued turn resumed quarantined thread: %s", lines[1])
			}
			if strings.Contains(lines[0], " resume ") != resumed {
				t.Fatal("fixture did not exercise requested initial session state")
			}
			session, err := os.ReadFile(cfg.SessionFile)
			if err != nil || strings.Contains(string(session), "bad-thread") {
				t.Fatalf("quarantined session persisted: %s, err=%v", session, err)
			}
		})
	}
}
