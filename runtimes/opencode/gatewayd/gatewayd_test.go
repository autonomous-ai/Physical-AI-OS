package gatewayd

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

const testToken = "test-token"

// successJSONL is the canned `opencode run --format json` output emitted by the
// fake binary (device-verified 1.18.4 shape): every line carries sessionID, the
// reply rides text.part.text, and a turn ends with step_finish (reason=stop) —
// there is NO session.idle from the CLI; the gatewayd synthesizes it on the
// process's clean exit.
const successJSONL = `{"type":"step_start","sessionID":"t123"}
{"type":"text","sessionID":"t123","part":{"text":"hello"}}
{"type":"step_finish","sessionID":"t123","part":{"reason":"stop","tokens":{"input":10,"output":5,"cache":{"read":0,"write":0}}}}`

// writeFakeOpenCode writes a bash script that appends its argv to argvFile
// (one "ARGV:<space-joined args>" line per invocation) and emits the canned
// success JSONL on stdout.
func writeFakeOpenCode(t *testing.T, dir, argvFile string) string {
	t.Helper()
	script := fmt.Sprintf("#!/bin/bash\necho \"ARGV:$*\" >> %q\ncat <<'EOF'\n%s\nEOF\n",
		argvFile, successJSONL)
	return writeScript(t, dir, "fake-opencode", script)
}

// writeFakeOpenCodeResumeFails is a variant that fails resume attempts with
// "session not found" on stderr and succeeds on fresh runs.
func writeFakeOpenCodeResumeFails(t *testing.T, dir, argvFile string) string {
	t.Helper()
	script := fmt.Sprintf(`#!/bin/bash
echo "ARGV:$*" >> %q
case "$*" in
  *--session*)
    echo "session not found" >&2
    exit 1
    ;;
esac
cat <<'EOF'
%s
EOF
`, argvFile, successJSONL)
	return writeScript(t, dir, "fake-opencode-resume-fails", script)
}

// writeFakeOpenCodeResumeSessionErr fails resume attempts with a terminal
// session.error JSONL frame on stdout (opencode's missing-session shape) + exit
// 1, and emits the canned success on fresh runs.
func writeFakeOpenCodeResumeSessionErr(t *testing.T, dir, argvFile string) string {
	t.Helper()
	script := fmt.Sprintf(`#!/bin/bash
echo "ARGV:$*" >> %q
case "$*" in
  *--session*)
    echo '{"type":"session.error","sessionID":"x","error":{"message":"session not found"}}'
    exit 1
    ;;
esac
cat <<'EOF'
%s
EOF
`, argvFile, successJSONL)
	return writeScript(t, dir, "fake-opencode-resume-session-err", script)
}

func writeScript(t *testing.T, dir, name, content string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0o755); err != nil {
		t.Fatalf("write fake opencode: %v", err)
	}
	return path
}

// startServer boots a Server on an ephemeral loopback port with all paths
// under t.TempDir().
func startServer(t *testing.T, opencodeBin string, dir string) (string, Config) {
	t.Helper()
	cfg := Config{
		Token:       testToken,
		Workspace:   filepath.Join(dir, "workspace"),
		Bin:         opencodeBin,
		SessionFile: filepath.Join(dir, "session.json"),
		AttachDir:   filepath.Join(dir, "attachments"),
		TurnTimeout: 30 * time.Second,
		Home:        dir,
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
	return "ws://" + ln.Addr().String() + "/opencode/ws", cfg
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

// readTurnFrames reads frames until session.idle/session.error/bridge.error,
// returning the opencode-event frames in order (bridge.status is skipped).
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
		if typ == "session.idle" || typ == "session.error" || typ == "bridge.error" {
			return frames
		}
	}
}

// argvLines returns the "ARGV:" lines the fake opencode appended per invocation.
func argvLines(t *testing.T, argvFile string) []string {
	t.Helper()
	data, err := os.ReadFile(argvFile)
	if err != nil {
		t.Fatalf("read argv file: %v", err)
	}
	var lines []string
	for _, l := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		if strings.HasPrefix(l, "ARGV:") {
			lines = append(lines, strings.TrimPrefix(l, "ARGV:"))
		}
	}
	return lines
}

func readSessionID(t *testing.T, sessionFile string) string {
	t.Helper()
	data, err := os.ReadFile(sessionFile)
	if err != nil {
		t.Fatalf("read session file: %v", err)
	}
	var sess struct {
		SessionID string `json:"session_id"`
	}
	if err := json.Unmarshal(data, &sess); err != nil {
		t.Fatalf("unmarshal session file %q: %v", data, err)
	}
	return sess.SessionID
}

func TestHappyPath(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	url, cfg := startServer(t, writeFakeOpenCode(t, dir, argvFile), dir)
	conn := dial(t, url, testToken)

	status := readFrame(t, conn)
	if status["type"] != "bridge.status" || status["state"] != "ready" {
		t.Fatalf("expected ready status, got %v", status)
	}
	if status["session_id"] != "" {
		t.Fatalf("expected empty session_id on fresh start, got %v", status["session_id"])
	}

	sendMessage(t, conn, "hi opencode")
	frames := readTurnFrames(t, conn)
	if len(frames) != 4 {
		t.Fatalf("expected 4 opencode frames, got %d: %v", len(frames), frames)
	}
	wantTypes := []string{"step_start", "text", "step_finish", "session.idle"}
	for i, want := range wantTypes {
		if frames[i]["type"] != want {
			t.Fatalf("frame %d: expected type %q, got %v", i, want, frames[i])
		}
	}
	if frames[0]["sessionID"] != "t123" {
		t.Fatalf("expected sessionID t123, got %v", frames[0]["sessionID"])
	}
	if frames[3]["sessionID"] != "t123" {
		t.Fatalf("expected synthesized session.idle sessionID t123, got %v", frames[3]["sessionID"])
	}
	if got := readSessionID(t, cfg.SessionFile); got != "t123" {
		t.Fatalf("session file: expected t123, got %q", got)
	}
	// The prompt must be the final positional argument of a fresh run.
	lines := argvLines(t, argvFile)
	if len(lines) != 1 || strings.Contains(lines[0], "--session") ||
		!strings.HasSuffix(lines[0], "hi opencode") {
		t.Fatalf("unexpected fresh argv: %v", lines)
	}
}

func TestResumeUsesStoredSessionID(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	url, _ := startServer(t, writeFakeOpenCode(t, dir, argvFile), dir)
	conn := dial(t, url, testToken)
	readFrame(t, conn)

	sendMessage(t, conn, "first")
	readTurnFrames(t, conn)
	sendMessage(t, conn, "second")
	readTurnFrames(t, conn)

	lines := argvLines(t, argvFile)
	if len(lines) != 2 {
		t.Fatalf("expected 2 opencode invocations, got %d: %v", len(lines), lines)
	}
	if strings.Contains(lines[0], "--session") {
		t.Fatalf("first run must be fresh, got argv: %s", lines[0])
	}
	if !strings.Contains(lines[1], "--session t123") {
		t.Fatalf("second run must resume t123, got argv: %s", lines[1])
	}
}

func TestSessionNewClearsSession(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	url, cfg := startServer(t, writeFakeOpenCode(t, dir, argvFile), dir)
	conn := dial(t, url, testToken)
	readFrame(t, conn)

	// session.new races an in-flight turn: it rides the same worker queue, so
	// it must execute AFTER the turn — the turn's sessionID re-persist cannot
	// undo the clear.
	sendMessage(t, conn, "first")
	if err := conn.WriteMessage(websocket.TextMessage, []byte(`{"type":"session.new"}`)); err != nil {
		t.Fatalf("send session.new: %v", err)
	}

	sawIdle := false
	for {
		frame := readFrame(t, conn)
		switch typ, _ := frame["type"].(string); typ {
		case "bridge.status":
			if frame["state"] != "session_cleared" {
				continue
			}
			if !sawIdle {
				t.Fatalf("session_cleared must arrive after session.idle, got %v early", frame)
			}
			if frame["session_id"] != "" {
				t.Fatalf("expected empty session_id in session_cleared, got %v", frame)
			}
		case "session.idle":
			sawIdle = true
			continue
		case "session.error", "bridge.error":
			t.Fatalf("unexpected failure frame: %v", frame)
		default:
			continue
		}
		break
	}
	if _, err := os.Stat(cfg.SessionFile); !os.IsNotExist(err) {
		t.Fatalf("session file should be deleted after session_cleared, stat err: %v", err)
	}

	sendMessage(t, conn, "after reset")
	readTurnFrames(t, conn)
	lines := argvLines(t, argvFile)
	if len(lines) != 2 {
		t.Fatalf("expected 2 opencode invocations, got %d: %v", len(lines), lines)
	}
	if strings.Contains(lines[0], "--session") {
		t.Fatalf("first run must be fresh, got argv: %s", lines[0])
	}
	if strings.Contains(lines[1], "--session") {
		t.Fatalf("run after session.new must be fresh, got argv: %s", lines[1])
	}
}

func TestAuthRejectsWrongToken(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	url, _ := startServer(t, writeFakeOpenCode(t, dir, argvFile), dir)

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

func TestResumeRetryFresh(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	sessionFile := filepath.Join(dir, "session.json")
	if err := os.WriteFile(sessionFile, []byte(`{"session_id":"stale-999"}`), 0o600); err != nil {
		t.Fatalf("seed session file: %v", err)
	}
	url, cfg := startServer(t, writeFakeOpenCodeResumeFails(t, dir, argvFile), dir)
	conn := dial(t, url, testToken)

	status := readFrame(t, conn)
	if status["session_id"] != "stale-999" {
		t.Fatalf("expected seeded session_id, got %v", status["session_id"])
	}

	sendMessage(t, conn, "retry me")
	frames := readTurnFrames(t, conn)
	last := frames[len(frames)-1]
	if last["type"] != "session.idle" {
		t.Fatalf("expected session.idle after fresh retry, got %v", frames)
	}

	lines := argvLines(t, argvFile)
	if len(lines) != 2 {
		t.Fatalf("expected resume attempt + fresh retry, got %d: %v", len(lines), lines)
	}
	if !strings.Contains(lines[0], "--session stale-999") {
		t.Fatalf("first run must resume stale id, got argv: %s", lines[0])
	}
	if strings.Contains(lines[1], "--session") {
		t.Fatalf("retry must be fresh, got argv: %s", lines[1])
	}
	if got := readSessionID(t, cfg.SessionFile); got != "t123" {
		t.Fatalf("session file after retry: expected t123, got %q", got)
	}
}

func TestResumedFailureFramesHeldBack(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	sessionFile := filepath.Join(dir, "session.json")
	if err := os.WriteFile(sessionFile, []byte(`{"session_id":"x"}`), 0o600); err != nil {
		t.Fatalf("seed session file: %v", err)
	}
	url, _ := startServer(t, writeFakeOpenCodeResumeSessionErr(t, dir, argvFile), dir)
	conn := dial(t, url, testToken)
	readFrame(t, conn)

	sendMessage(t, conn, "retry me")
	frames := readTurnFrames(t, conn)
	idle := 0
	for _, f := range frames {
		switch f["type"] {
		case "session.error", "bridge.error":
			// The resumed attempt's terminal failure must be held back — the
			// fresh retry owns the turn on the client side.
			t.Fatalf("client must not see the resumed attempt's failure: %v", f)
		case "session.idle":
			idle++
		}
	}
	if idle != 1 {
		t.Fatalf("expected exactly one session.idle, got %d: %v", idle, frames)
	}

	// Nothing stashed may trail the completed turn: a ping must be answered next.
	if err := conn.WriteMessage(websocket.TextMessage, []byte(`{"type":"ping","id":9}`)); err != nil {
		t.Fatalf("send ping: %v", err)
	}
	if frame := readFrame(t, conn); frame["type"] != "pong" {
		t.Fatalf("expected pong right after session.idle, got %v", frame)
	}

	lines := argvLines(t, argvFile)
	if len(lines) != 2 {
		t.Fatalf("expected resume attempt + fresh retry, got %d: %v", len(lines), lines)
	}
	if !strings.Contains(lines[0], "--session x") {
		t.Fatalf("first run must resume stale id, got argv: %s", lines[0])
	}
	if strings.Contains(lines[1], "--session") {
		t.Fatalf("retry must be fresh, got argv: %s", lines[1])
	}
}
