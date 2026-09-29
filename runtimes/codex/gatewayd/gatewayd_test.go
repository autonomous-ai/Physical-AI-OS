package gatewayd

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

const testToken = "test-token"

// successJSONL remains for unit tests of the retired exec parser.
const successJSONL = `{"type":"thread.started","thread_id":"t123"}
{"type":"item.completed","item":{"item_type":"agent_message","text":"hello"}}
{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}`

// writeFakeCodex emulates the App Server JSON-RPC subset gatewayd uses.
func writeFakeCodex(t *testing.T, dir, argvFile string) string {
	t.Helper()
	script := fmt.Sprintf("#!/bin/bash\nGO_WANT_CODEX_APP_FAKE=1 CODEX_APP_FAKE_LOG=%q exec %q -test.run=TestCodexAppServerFake -- \"$@\"\n", argvFile, os.Args[0])
	return writeScript(t, dir, "fake-codex", script)
}

// TestCodexAppServerFake is launched by writeFakeCodex as a subprocess.
func TestCodexAppServerFake(t *testing.T) {
	if os.Getenv("GO_WANT_CODEX_APP_FAKE") != "1" {
		return
	}
	logFile := os.Getenv("CODEX_APP_FAKE_LOG")
	write := func(v any) { b, _ := json.Marshal(v); _, _ = os.Stdout.Write(append(b, '\n')) }
	sc := bufio.NewScanner(os.Stdin)
	for sc.Scan() {
		var req struct {
			ID     int    `json:"id"`
			Method string `json:"method"`
		}
		_ = json.Unmarshal(sc.Bytes(), &req)
		if req.Method != "" {
			_ = os.WriteFile(logFile, append([]byte{}, append(readFileOrNil(logFile), []byte("METHOD:"+req.Method+"\n")...)...), 0o600)
		}
		switch req.Method {
		case "initialize":
			write(map[string]any{"id": req.ID, "result": map[string]any{}})
		case "thread/start":
			write(map[string]any{"id": req.ID, "result": map[string]any{"thread": map[string]any{"id": "t123"}}})
		case "turn/start":
			write(map[string]any{"id": req.ID, "result": map[string]any{"turn": map[string]any{"id": "turn-1"}}})
			go func() {
				time.Sleep(120 * time.Millisecond)
				write(map[string]any{"method": "turn/started", "params": map[string]any{"threadId": "t123", "turn": map[string]any{"id": "turn-1"}}})
				write(map[string]any{"method": "item/completed", "params": map[string]any{"threadId": "t123", "turnId": "turn-1", "item": map[string]any{"id": "m1", "type": "agentMessage", "text": "hello"}}})
				// codex-rs 0.150.1 shape: usage arrives on its own camelCase
				// notification BEFORE the terminal event, never inside it.
				write(map[string]any{"method": "thread/tokenUsage/updated", "params": map[string]any{"threadId": "t123", "turnId": "turn-1",
					"tokenUsage": map[string]any{
						"total": map[string]any{"inputTokens": 99999, "cachedInputTokens": 88888, "outputTokens": 999},
						"last":  map[string]any{"inputTokens": 723, "cachedInputTokens": 30720, "cacheWriteInputTokens": 0, "outputTokens": 31},
					}}})
				write(map[string]any{"method": "turn/completed", "params": map[string]any{"threadId": "t123", "turn": map[string]any{"id": "turn-1", "status": "completed"}}})
			}()
		case "turn/steer":
			write(map[string]any{"id": req.ID, "result": map[string]any{}})
		case "turn/interrupt":
			write(map[string]any{"id": req.ID, "result": map[string]any{}})
			write(map[string]any{"method": "turn/completed", "params": map[string]any{"threadId": "t123", "turn": map[string]any{"id": "turn-1", "status": "interrupted"}}})
		}
	}
	os.Exit(0)
}

func readFileOrNil(path string) []byte { b, _ := os.ReadFile(path); return b }

// writeFakeCodexResumeFails is a variant that fails resume attempts with
// "session not found" on stderr and succeeds on fresh runs.
func writeFakeCodexResumeFails(t *testing.T, dir, argvFile string) string {
	t.Helper()
	script := fmt.Sprintf(`#!/bin/bash
echo "ARGV:$*" >> %q
case "$*" in
  *resume*)
    echo "session not found" >&2
    exit 1
    ;;
esac
cat <<'EOF'
%s
EOF
`, argvFile, successJSONL)
	return writeScript(t, dir, "fake-codex-resume-fails", script)
}

func writeScript(t *testing.T, dir, name, content string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0o755); err != nil {
		t.Fatalf("write fake codex: %v", err)
	}
	return path
}

// startServer boots a Server on an ephemeral loopback port with all paths
// under t.TempDir().
func startServer(t *testing.T, codexBin string, dir string) (string, Config) {
	t.Helper()
	return startServerTimeout(t, codexBin, dir, 30*time.Second)
}

// startServerTimeout is startServer with an explicit per-turn timeout, for the
// tests that need a turn to actually hit it.
func startServerTimeout(t *testing.T, codexBin string, dir string, turnTimeout time.Duration) (string, Config) {
	t.Helper()
	cfg := Config{
		Token:        testToken,
		Workspace:    filepath.Join(dir, "workspace"),
		CodexBin:     codexBin,
		CodexHome:    dir,
		SessionFile:  filepath.Join(dir, "session.json"),
		AttachDir:    filepath.Join(dir, "attachments"),
		TurnTimeout:  turnTimeout,
		Home:         dir,
		UseAppServer: true,
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	srv := New(cfg, ln)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		if err := srv.Serve(ctx); err != nil {
			t.Errorf("serve: %v", err)
		}
	}()
	t.Cleanup(func() {
		cancel()
		<-done
	})
	return "ws://" + ln.Addr().String() + "/codex/ws", cfg
}

func dial(t *testing.T, url, token string) *websocket.Conn {
	t.Helper()
	header := http.Header{"Authorization": {"Bearer " + token}}
	conn, _, err := websocket.DefaultDialer.Dial(url, header)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

func readFrame(t *testing.T, conn *websocket.Conn) map[string]any {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	_, data, err := conn.ReadMessage()
	if err != nil {
		t.Fatalf("read frame: %v", err)
	}
	var frame map[string]any
	if err := json.Unmarshal(data, &frame); err != nil {
		t.Fatalf("unmarshal frame %q: %v", data, err)
	}
	return frame
}

func sendMessage(t *testing.T, conn *websocket.Conn, content string) {
	t.Helper()
	frame := fmt.Sprintf(`{"type":"message.send","id":1,"payload":{"content":%q}}`, content)
	if err := conn.WriteMessage(websocket.TextMessage, []byte(frame)); err != nil {
		t.Fatalf("send message.send: %v", err)
	}
}

// readTurnFrames reads frames until turn.completed/turn.failed/bridge.error,
// returning the codex-event frames in order (bridge.status is skipped).
func readTurnFrames(t *testing.T, conn *websocket.Conn) []map[string]any {
	t.Helper()
	var frames []map[string]any
	for {
		frame := readFrame(t, conn)
		typ, _ := frame["type"].(string)
		if typ == "bridge.status" {
			continue
		}
		frames = append(frames, frame)
		if typ == "turn.completed" || typ == "turn.failed" || typ == "bridge.error" {
			return frames
		}
	}
}

func TestAuthRejectsWrongToken(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	url, _ := startServer(t, writeFakeCodex(t, dir, argvFile), dir)

	header := http.Header{"Authorization": {"Bearer wrong-token"}}
	conn, _, err := websocket.DefaultDialer.Dial(url, header)
	if err != nil {
		return
	}
	defer conn.Close()
	_ = conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	_, _, err = conn.ReadMessage()
	if err == nil {
		t.Fatal("expected connection to be closed for wrong token")
	}
	var closeErr *websocket.CloseError
	if ok := websocket.IsCloseError(err, closeUnauthorized); !ok {
		if e, isClose := err.(*websocket.CloseError); isClose {
			closeErr = e
		}
		t.Fatalf("expected close code %d, got %v (close: %v)", closeUnauthorized, err, closeErr)
	}
}
