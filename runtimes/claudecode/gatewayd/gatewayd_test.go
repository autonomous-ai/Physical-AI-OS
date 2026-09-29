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

// assistantLine / resultLine are the canned stream-json events the fake claude
// emits per stdin line — the happy-path test compares the forwarded frame
// bytes VERBATIM against assistantLine.
const (
	assistantLine = `{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"hello"}]}}`
	resultLine    = `{"type":"result","subtype":"success","is_error":false,"result":"hello"}`
)

// writeFakeClaude writes a bash script standing in for the persistent claude
// child: it appends its argv (and $FOO from the env) to argvFile once per
// SPAWN, emits a stream-json init event with a per-spawn session id, then per
// stdin LINE logs the line to stdinFile, bumps the counter in countFile, and
// emits an assistant + result event.
func writeFakeClaude(t *testing.T, dir, argvFile, countFile, stdinFile string) string {
	t.Helper()
	script := fmt.Sprintf(`#!/bin/bash
echo "ARGV:$*" >> %[1]q
echo "ENV:$FOO" >> %[1]q
echo '{"type":"system","subtype":"init","session_id":"sess-'"$$"'"}'
while IFS= read -r line; do
  printf '%%s\n' "$line" >> %[2]q
  n=0
  [ -f %[3]q ] && n=$(cat %[3]q)
  echo $((n+1)) > %[3]q
  echo '%[4]s'
  echo '%[5]s'
done
`, argvFile, stdinFile, countFile, assistantLine, resultLine)
	return writeScript(t, dir, "fake-claude", script)
}

// writeFakeClaudeCrashAfterResult is a variant that answers ONE turn (emitting
// the result, so nothing is left in flight) and then exits — the respawn must
// --resume the persisted session id.
func writeFakeClaudeCrashAfterResult(t *testing.T, dir, argvFile string) string {
	t.Helper()
	script := fmt.Sprintf(`#!/bin/bash
echo "ARGV:$*" >> %[1]q
echo '{"type":"system","subtype":"init","session_id":"sess-'"$$"'"}'
IFS= read -r line || exit 0
echo '%[2]s'
exit 3
`, argvFile, resultLine)
	return writeScript(t, dir, "fake-claude-crash", script)
}

// writeFakeClaudeExitMidTurn is a variant that exits WITHOUT emitting a result
// for the received turn — the gatewayd must surface a bridge.error.
func writeFakeClaudeExitMidTurn(t *testing.T, dir, argvFile string) string {
	t.Helper()
	script := fmt.Sprintf(`#!/bin/bash
echo "ARGV:$*" >> %q
echo '{"type":"system","subtype":"init","session_id":"sess-mid"}'
IFS= read -r line
exit 7
`, argvFile)
	return writeScript(t, dir, "fake-claude-midturn", script)
}

// writeFakeClaudeStaleResume stands in for a claude whose --resume target was
// dropped from its store: spawned WITH --resume it prints the stale-session
// marker to stderr and exits rc=1; spawned fresh it inits with a per-spawn
// session id and stays alive reading stdin.
func writeFakeClaudeStaleResume(t *testing.T, dir, argvFile string) string {
	t.Helper()
	script := fmt.Sprintf(`#!/bin/bash
echo "ARGV:$*" >> %[1]q
for a in "$@"; do
  if [ "$a" = "--resume" ]; then
    echo "No conversation found with session ID: dead-session" >&2
    exit 1
  fi
done
echo '{"type":"system","subtype":"init","session_id":"sess-'"$$"'"}'
while IFS= read -r line; do :; done
`, argvFile)
	return writeScript(t, dir, "fake-claude-stale", script)
}

func writeScript(t *testing.T, dir, name, content string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0o755); err != nil {
		t.Fatalf("write fake claude: %v", err)
	}
	return path
}

// startServer boots a Server on an ephemeral loopback port with all paths
// under dir.
func startServer(t *testing.T, claudeBin, dir string, backoff time.Duration) (string, Config) {
	t.Helper()
	cfg := Config{
		Token:          testToken,
		Workspace:      filepath.Join(dir, "workspace"),
		EnvFile:        filepath.Join(dir, ".env"),
		SessionFile:    filepath.Join(dir, "session.json"),
		ClaudeBin:      claudeBin,
		RestartBackoff: backoff,
		Home:           dir,
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
	return "ws://" + ln.Addr().String() + "/claude/ws", cfg
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

func readRawFrame(t *testing.T, conn *websocket.Conn) []byte {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	_, data, err := conn.ReadMessage()
	if err != nil {
		t.Fatalf("read frame: %v", err)
	}
	return data
}

func parseFrame(t *testing.T, data []byte) map[string]any {
	t.Helper()
	var frame map[string]any
	if err := json.Unmarshal(data, &frame); err != nil {
		t.Fatalf("unmarshal frame %q: %v", data, err)
	}
	return frame
}

// nextFrameSkipping reads frames until one whose type is not in skip.
func nextFrameSkipping(t *testing.T, conn *websocket.Conn, skip ...string) (map[string]any, []byte) {
	t.Helper()
	for {
		raw := readRawFrame(t, conn)
		frame := parseFrame(t, raw)
		typ, _ := frame["type"].(string)
		skipped := false
		for _, sk := range skip {
			if typ == sk {
				skipped = true
				break
			}
		}
		if !skipped {
			return frame, raw
		}
	}
}

func sendFrame(t *testing.T, conn *websocket.Conn, frame string) {
	t.Helper()
	if err := conn.WriteMessage(websocket.TextMessage, []byte(frame)); err != nil {
		t.Fatalf("send frame: %v", err)
	}
}

func sendMessage(t *testing.T, conn *websocket.Conn, content string) {
	t.Helper()
	sendFrame(t, conn, fmt.Sprintf(`{"type":"message.send","id":1,"payload":{"content":%q}}`, content))
}

// readTurn reads until the result event, returning the raw claude frames in
// order (bridge.status and stray system frames skipped).
func readTurn(t *testing.T, conn *websocket.Conn) [][]byte {
	t.Helper()
	var frames [][]byte
	for {
		frame, raw := nextFrameSkipping(t, conn, "bridge.status", "system")
		frames = append(frames, raw)
		if typ, _ := frame["type"].(string); typ == "result" || typ == "bridge.error" {
			return frames
		}
	}
}

// argvLines returns the "ARGV:" lines the fake claude appended per spawn.
func argvLines(t *testing.T, argvFile string) []string {
	t.Helper()
	data, err := os.ReadFile(argvFile)
	if err != nil {
		return nil
	}
	var lines []string
	for _, l := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		if strings.HasPrefix(l, "ARGV:") {
			lines = append(lines, strings.TrimPrefix(l, "ARGV:"))
		}
	}
	return lines
}

// readSessionID returns the persisted session id ("" while absent).
func readSessionID(sessionFile string) string {
	data, err := os.ReadFile(sessionFile)
	if err != nil {
		return ""
	}
	var sess struct {
		SessionID string `json:"session_id"`
	}
	if json.Unmarshal(data, &sess) != nil {
		return ""
	}
	return sess.SessionID
}

// waitFor polls cond until it holds or the timeout expires.
func waitFor(t *testing.T, what string, timeout time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("timeout waiting for %s", what)
}

const baseArgv = "--print --verbose --input-format stream-json --output-format stream-json --dangerously-skip-permissions"

func TestHappyPath(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	countFile := filepath.Join(dir, "count.txt")
	stdinFile := filepath.Join(dir, "stdin.txt")
	envFile := "# managed env\nFOO = \"bar\"\njunk line without equals\n" +
		"CLAUDECODE_CHANNELS=\"plugin:telegram@claude-plugins-official\"\n"
	if err := os.WriteFile(filepath.Join(dir, ".env"), []byte(envFile), 0o600); err != nil {
		t.Fatalf("write env file: %v", err)
	}
	bin := writeFakeClaude(t, dir, argvFile, countFile, stdinFile)
	url, cfg := startServer(t, bin, dir, 50*time.Millisecond)
	conn := dial(t, url, testToken)

	status := parseFrame(t, readRawFrame(t, conn))
	if status["type"] != "bridge.status" {
		t.Fatalf("expected bridge.status on connect, got %v", status)
	}
	payload, ok := status["payload"].(map[string]any)
	if !ok {
		t.Fatalf("bridge.status payload missing: %v", status)
	}
	if _, ok := payload["claude_running"].(bool); !ok {
		t.Fatalf("bridge.status payload.claude_running not a bool: %v", payload)
	}
	if _, present := payload["session_id"]; !present {
		t.Fatalf("bridge.status payload.session_id missing: %v", payload)
	}

	waitFor(t, "session file", 5*time.Second, func() bool { return readSessionID(cfg.SessionFile) != "" })
	sid := readSessionID(cfg.SessionFile)
	if !strings.HasPrefix(sid, "sess-") {
		t.Fatalf("unexpected persisted session id %q", sid)
	}

	sendFrame(t, conn, `{"type":"message.send","id":1,"payload":{`+
		`"content":"hi claude","attachments":[`+
		`{"type":"image","url":"data:image/png;base64,aGVsbG8="},`+
		`{"type":"image","url":"data:image/png;base64,!!!"}]}}`)
	frames := readTurn(t, conn)
	if len(frames) != 2 {
		t.Fatalf("expected assistant + result, got %d frames: %s", len(frames), frames)
	}
	if string(frames[0]) != assistantLine {
		t.Fatalf("assistant frame not verbatim:\n got %s\nwant %s", frames[0], assistantLine)
	}
	if string(frames[1]) != resultLine {
		t.Fatalf("result frame not verbatim:\n got %s\nwant %s", frames[1], resultLine)
	}

	// The stdin line is a stream-json user message with text + the valid image
	// block only (the bad attachment is skipped).
	stdinData, err := os.ReadFile(stdinFile)
	if err != nil {
		t.Fatalf("read stdin log: %v", err)
	}
	stdin := string(stdinData)
	for _, want := range []string{`"type":"user"`, `"role":"user"`, `"text":"hi claude"`,
		`"media_type":"image/png"`, `"data":"aGVsbG8="`} {
		if !strings.Contains(stdin, want) {
			t.Fatalf("stdin line missing %s: %s", want, stdin)
		}
	}
	if strings.Contains(stdin, "!!!") {
		t.Fatalf("invalid attachment must be skipped, stdin: %s", stdin)
	}

	lines := argvLines(t, argvFile)
	if len(lines) != 1 {
		t.Fatalf("expected 1 spawn, got %d: %v", len(lines), lines)
	}
	if !strings.Contains(lines[0], baseArgv) {
		t.Fatalf("argv missing base flags: %s", lines[0])
	}
	if strings.Contains(lines[0], "--resume") {
		t.Fatalf("fresh spawn must not resume: %s", lines[0])
	}
	if !strings.Contains(lines[0], "--channels plugin:telegram@claude-plugins-official") {
		t.Fatalf("argv missing channels: %s", lines[0])
	}
	if data, _ := os.ReadFile(argvFile); !strings.Contains(string(data), "ENV:bar") {
		t.Fatalf("env file FOO not in child env: %s", data)
	}

	sendFrame(t, conn, `{"type":"ping","id":9}`)
	pong, raw := nextFrameSkipping(t, conn, "bridge.status")
	if pong["type"] != "pong" || len(pong) != 1 {
		t.Fatalf("expected bare pong, got %s", raw)
	}
}

func TestSecondTurnReusesChild(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	countFile := filepath.Join(dir, "count.txt")
	bin := writeFakeClaude(t, dir, argvFile, countFile, filepath.Join(dir, "stdin.txt"))
	url, _ := startServer(t, bin, dir, 50*time.Millisecond)
	conn := dial(t, url, testToken)

	sendMessage(t, conn, "first")
	readTurn(t, conn)
	sendMessage(t, conn, "second")
	readTurn(t, conn)

	if lines := argvLines(t, argvFile); len(lines) != 1 {
		t.Fatalf("expected 1 spawn for 2 turns, got %d: %v", len(lines), lines)
	}
	count, err := os.ReadFile(countFile)
	if err != nil {
		t.Fatalf("read count file: %v", err)
	}
	if got := strings.TrimSpace(string(count)); got != "2" {
		t.Fatalf("expected the same child to handle 2 turns, count=%s", got)
	}
}

func TestSessionNewRestartsFresh(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	bin := writeFakeClaude(t, dir, argvFile, filepath.Join(dir, "count.txt"), filepath.Join(dir, "stdin.txt"))
	url, cfg := startServer(t, bin, dir, 300*time.Millisecond)
	conn := dial(t, url, testToken)

	waitFor(t, "session file", 5*time.Second, func() bool { return readSessionID(cfg.SessionFile) != "" })
	oldSID := readSessionID(cfg.SessionFile)

	sendMessage(t, conn, "first")
	readTurn(t, conn)

	sendFrame(t, conn, `{"type":"session.new"}`)
	waitFor(t, "session file removal", 5*time.Second, func() bool {
		_, err := os.Stat(cfg.SessionFile)
		return os.IsNotExist(err)
	})
	waitFor(t, "fresh respawn", 5*time.Second, func() bool { return len(argvLines(t, argvFile)) == 2 })

	lines := argvLines(t, argvFile)
	if strings.Contains(lines[0], "--resume") {
		t.Fatalf("first spawn must be fresh: %s", lines[0])
	}
	if strings.Contains(lines[1], "--resume") {
		t.Fatalf("spawn after session.new must be fresh: %s", lines[1])
	}
	waitFor(t, "new session id", 5*time.Second, func() bool {
		sid := readSessionID(cfg.SessionFile)
		return sid != "" && sid != oldSID
	})
}

func TestChildCrashRespawnsWithResume(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	bin := writeFakeClaudeCrashAfterResult(t, dir, argvFile)
	url, cfg := startServer(t, bin, dir, 50*time.Millisecond)
	conn := dial(t, url, testToken)

	waitFor(t, "session file", 5*time.Second, func() bool { return readSessionID(cfg.SessionFile) != "" })
	sid := readSessionID(cfg.SessionFile)

	sendMessage(t, conn, "one")
	readTurn(t, conn)

	waitFor(t, "respawn", 5*time.Second, func() bool { return len(argvLines(t, argvFile)) >= 2 })
	lines := argvLines(t, argvFile)
	if strings.Contains(lines[0], "--resume") {
		t.Fatalf("first spawn must be fresh: %s", lines[0])
	}
	if !strings.Contains(lines[1], "--resume "+sid) {
		t.Fatalf("respawn must resume %s: %s", sid, lines[1])
	}
}

func TestStaleResumeSelfHeals(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	if err := os.WriteFile(filepath.Join(dir, "session.json"),
		[]byte(`{"session_id":"dead-session"}`), 0o600); err != nil {
		t.Fatalf("seed session file: %v", err)
	}
	bin := writeFakeClaudeStaleResume(t, dir, argvFile)
	url, cfg := startServer(t, bin, dir, 50*time.Millisecond)
	_ = dial(t, url, testToken)

	waitFor(t, "fresh session after self-heal", 5*time.Second, func() bool {
		sid := readSessionID(cfg.SessionFile)
		return strings.HasPrefix(sid, "sess-")
	})

	lines := argvLines(t, argvFile)
	if len(lines) < 2 {
		t.Fatalf("expected a resume attempt then a fresh respawn, got %v", lines)
	}
	if !strings.Contains(lines[0], "--resume dead-session") {
		t.Fatalf("first spawn should resume the seeded dead id: %s", lines[0])
	}
	// Every spawn after the self-heal must be fresh (no --resume) until a live
	// id is captured.
	if strings.Contains(lines[len(lines)-1], "--resume") {
		t.Fatalf("spawn after self-heal must be fresh: %s", lines[len(lines)-1])
	}
}

func TestChildExitMidTurnEmitsBridgeError(t *testing.T) {
	dir := t.TempDir()
	argvFile := filepath.Join(dir, "argv.txt")
	bin := writeFakeClaudeExitMidTurn(t, dir, argvFile)
	url, _ := startServer(t, bin, dir, 100*time.Millisecond)
	conn := dial(t, url, testToken)

	sendMessage(t, conn, "doomed")
	for {
		frame, raw := nextFrameSkipping(t, conn, "bridge.status", "system")
		typ, _ := frame["type"].(string)
		if typ == "result" {
			t.Fatalf("no result expected from a mid-turn exit, got %s", raw)
		}
		if typ != "bridge.error" {
			continue
		}
		payload, _ := frame["payload"].(map[string]any)
		if payload == nil || payload["message"] != "claude exited (rc=7) mid-turn" {
			t.Fatalf("unexpected bridge.error payload: %s", raw)
		}
		return
	}
}

func TestAuthRejectsWrongToken(t *testing.T) {
	dir := t.TempDir()
	bin := writeFakeClaude(t, dir, filepath.Join(dir, "argv.txt"),
		filepath.Join(dir, "count.txt"), filepath.Join(dir, "stdin.txt"))
	url, _ := startServer(t, bin, dir, 50*time.Millisecond)

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
	if !websocket.IsCloseError(err, closeUnauthorized) {
		t.Fatalf("expected close code %d, got %v", closeUnauthorized, err)
	}
}
