package opencode

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

type codingTestRig struct {
	svc  *OpenCodeService
	dms  chan string
	runs chan codingRunCall
}

type codingRunCall struct {
	folder, sessionID, prompt string
}

func newCodingRig(t *testing.T) *codingTestRig {
	t.Helper()
	dms := make(chan string, 16)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/sendMessage") {
			body, _ := io.ReadAll(r.Body)
			var p struct {
				Text string `json:"text"`
			}
			_ = json.Unmarshal(body, &p)
			dms <- p.Text
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	t.Cleanup(srv.Close)

	rig := &codingTestRig{dms: dms, runs: make(chan codingRunCall, 8)}
	rig.svc = &OpenCodeService{
		config:                &config.Config{TelegramBotToken: "T", TelegramUserID: "9"},
		telegramAPIBase:       srv.URL,
		codingSelPath:         filepath.Join(t.TempDir(), "sel.json"),
		folderHasLiveOpenCode: func(string) bool { return false },
	}
	rig.svc.codingRunner = func(_ context.Context, folder, sessionID, prompt string) (string, string, error) {
		rig.runs <- codingRunCall{folder, sessionID, prompt}
		return "ok reply for " + prompt, "new-thread-1234", nil
	}
	return rig
}

func (r *codingTestRig) waitDM(t *testing.T) string {
	t.Helper()
	select {
	case s := <-r.dms:
		return s
	case <-time.After(3 * time.Second):
		t.Fatal("no Telegram DM within 3s")
		return ""
	}
}

// TestCodingCommandsFlow drives the /new → plain-message → /device path.
func TestCodingCommandsFlow(t *testing.T) {
	rig := newCodingRig(t)
	s := rig.svc
	ctx := context.Background()
	chat := "9"
	proj := t.TempDir()

	if !s.handleTelegramCoding(ctx, "/new "+proj, chat) {
		t.Fatal("/new should be handled")
	}
	if dm := rig.waitDM(t); !strings.Contains(dm, "New") {
		t.Fatalf("/new DM = %q", dm)
	}
	tgt, ok := s.getCodingTarget(chat)
	if !ok || tgt.Folder != proj || tgt.SessionID != "" {
		t.Fatalf("selection after /new = %+v ok=%v", tgt, ok)
	}

	if !s.handleTelegramCoding(ctx, "add undo button", chat) {
		t.Fatal("plain msg with active selection should be handled")
	}
	select {
	case call := <-rig.runs:
		if call.folder != proj || call.sessionID != "" || call.prompt != "add undo button" {
			t.Fatalf("runner called with %+v", call)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("coding runner not invoked")
	}
	if dm := rig.waitDM(t); !strings.Contains(dm, "ok reply for add undo button") {
		t.Fatalf("reply DM = %q", dm)
	}
	if tgt, _ := s.getCodingTarget(chat); tgt.SessionID != "new-thread-1234" {
		t.Errorf("session id not updated: %+v", tgt)
	}

	s.handleTelegramCoding(ctx, "/device", chat)
	rig.waitDM(t)
	if s.handleTelegramCoding(ctx, "hi lamp", chat) {
		t.Fatal("after /device a plain msg must fall through to device-main (return false)")
	}
}

// TestResumeCommand confirms the /resume flow degrades honestly: with discovery
// off, an empty list is reported and /resume <folder> points the user at /new.
func TestResumeCommand(t *testing.T) {
	rig := newCodingRig(t)
	s := rig.svc
	ctx := context.Background()
	chat := "9"

	s.handleTelegramCoding(ctx, "/resume", chat)
	if dm := rig.waitDM(t); !strings.Contains(dm, "No coding") {
		t.Fatalf("/resume empty-list DM = %q", dm)
	}

	s.handleTelegramCoding(ctx, "/resume /root/nope", chat)
	if dm := rig.waitDM(t); !strings.Contains(dm, "/new") {
		t.Fatalf("/resume <folder> DM = %q", dm)
	}
	if _, ok := s.getCodingTarget(chat); ok {
		t.Fatal("no selection should be set when nothing was resumable")
	}
}

func TestCodingSelectionPersists(t *testing.T) {
	rig := newCodingRig(t)
	rig.svc.setCodingTarget("9", codingTarget{Folder: "/root/app", SessionID: "th0"})

	s2 := &OpenCodeService{codingSelPath: rig.svc.codingSelPath}
	tgt, ok := s2.getCodingTarget("9")
	if !ok || tgt.Folder != "/root/app" || tgt.SessionID != "th0" {
		t.Fatalf("persisted selection not recovered: %+v ok=%v", tgt, ok)
	}
}

func TestCodingLiveTUIGuard(t *testing.T) {
	rig := newCodingRig(t)
	rig.svc.folderHasLiveOpenCode = func(string) bool { return true }
	ran := false
	rig.svc.codingRunner = func(context.Context, string, string, string) (string, string, error) {
		ran = true
		return "", "", nil
	}
	rig.svc.setCodingTarget("9", codingTarget{Folder: "/root/live", SessionID: "s"})
	rig.svc.handleTelegramCoding(context.Background(), "do something", "9")
	if dm := rig.waitDM(t); !strings.Contains(dm, "terminal") {
		t.Fatalf("guard DM = %q", dm)
	}
	if ran {
		t.Fatal("runner must NOT run while an interactive TUI holds the folder")
	}
}

// TestParseOpenCodeResult feeds real `opencode run --format json` JSONL: text
// events accumulate, sessionID is captured, session.idle ends the turn, and a
// session.error surfaces the message.
func TestParseOpenCodeResult(t *testing.T) {
	ok := []byte(`{"type":"step_start","sessionID":"s"}
{"type":"reasoning","sessionID":"s","text":"thinking"}
{"type":"text","sessionID":"s","text":"hi"}
{"type":"session.idle","sessionID":"s"}`)
	reply, id, terr := parseOpenCodeResult(ok)
	if reply != "hi" || id != "s" || terr != "" {
		t.Fatalf("ok: reply=%q id=%q terr=%q", reply, id, terr)
	}

	multi := []byte(`{"type":"text","sessionID":"s2","text":"part1 "}
{"type":"text","sessionID":"s2","part":{"text":"part2"}}
{"type":"session.idle","sessionID":"s2"}`)
	if reply, id, _ := parseOpenCodeResult(multi); reply != "part1 part2" || id != "s2" {
		t.Fatalf("multi: reply=%q id=%q", reply, id)
	}

	failed := []byte(`{"type":"text","sessionID":"s3"}
{"type":"session.error","sessionID":"s3","error":{"message":"404 boom"}}`)
	if reply, id, terr := parseOpenCodeResult(failed); reply != "" || id != "s3" || terr != "404 boom" {
		t.Fatalf("failed: reply=%q id=%q terr=%q", reply, id, terr)
	}

	strErr := []byte(`{"type":"error","sessionID":"s4","error":"kaboom"}`)
	if _, _, terr := parseOpenCodeResult(strErr); terr != "kaboom" {
		t.Fatalf("strErr: terr=%q, want kaboom", terr)
	}
}

func TestChunkString(t *testing.T) {
	if got := chunkString("short", 4000); len(got) != 1 || got[0] != "short" {
		t.Fatalf("short unchanged: %v", got)
	}
	long := strings.Repeat("a", 100) + "\n" + strings.Repeat("b", 100)
	parts := chunkString(long, 120)
	if len(parts) < 2 || strings.Join(parts, "") != long {
		t.Fatalf("chunks wrong: %d parts, reassembly mismatch", len(parts))
	}
	for _, p := range parts {
		if len([]rune(p)) > 120 {
			t.Fatalf("chunk exceeds limit: %d", len([]rune(p)))
		}
	}
}

func TestLoadEnvFilePairs(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ".env")
	if err := os.WriteFile(path, []byte("# c\nLLM_API_KEY = \"sk-1\"\nbad line\nX=y\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	pairs := loadEnvFilePairs(path)
	want := map[string]bool{"LLM_API_KEY=sk-1": true, "X=y": true}
	if len(pairs) != len(want) {
		t.Fatalf("pairs = %v", pairs)
	}
	for _, p := range pairs {
		if !want[p] {
			t.Errorf("unexpected pair %q", p)
		}
	}
}
