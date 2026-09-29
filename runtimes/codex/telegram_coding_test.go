package codex

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
	svc  *CodexService
	dms  chan string
	runs chan codingRunCall
}

type codingRunCall struct {
	folder, sessionID, prompt string
}

func newCodingRig(t *testing.T, sessionsDir string) *codingTestRig {
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
	rig.svc = &CodexService{
		config:               &config.Config{TelegramBotToken: "T", TelegramUserID: "9"},
		telegramAPIBase:      srv.URL,
		codexSessionsDirPath: sessionsDir,
		codingSelPath:        filepath.Join(t.TempDir(), "sel.json"),
		folderHasLiveCodex:   func(string) bool { return false },
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

func TestCodingCommandsFlow(t *testing.T) {
	proj := t.TempDir()
	writeRollout(t, proj, "aaaa1111", "/root/test", "caro game", time.Now())
	rig := newCodingRig(t, proj)
	s := rig.svc
	ctx := context.Background()
	chat := "9"

	if !s.handleTelegramCoding(ctx, "/sessions", chat) {
		t.Fatal("/sessions should be handled")
	}
	if dm := rig.waitDM(t); !strings.Contains(dm, "/root/test") || !strings.Contains(dm, "caro game") {
		t.Fatalf("/sessions DM missing folder/summary: %q", dm)
	}

	s.handleTelegramCoding(ctx, "/use 1", chat)
	if dm := rig.waitDM(t); !strings.Contains(dm, "In thread") {
		t.Fatalf("/use DM = %q", dm)
	}
	tgt, ok := s.getCodingTarget(chat)
	if !ok || tgt.Folder != "/root/test" || tgt.SessionID != "aaaa1111" {
		t.Fatalf("selection = %+v ok=%v", tgt, ok)
	}

	if !s.handleTelegramCoding(ctx, "add undo button", chat) {
		t.Fatal("plain msg with active selection should be handled")
	}
	select {
	case call := <-rig.runs:
		if call.folder != "/root/test" || call.sessionID != "aaaa1111" || call.prompt != "add undo button" {
			t.Fatalf("runner called with %+v", call)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("coding runner not invoked")
	}
	if dm := rig.waitDM(t); !strings.Contains(dm, "ok reply for add undo button") {
		t.Fatalf("reply DM = %q", dm)
	}
	if tgt, _ := s.getCodingTarget(chat); tgt.SessionID != "new-thread-1234" {
		t.Errorf("thread id not updated: %+v", tgt)
	}

	s.handleTelegramCoding(ctx, "/device", chat)
	rig.waitDM(t)
	if s.handleTelegramCoding(ctx, "hi lamp", chat) {
		t.Fatal("after /device a plain msg must fall through to device-main (return false)")
	}
}

func TestResumeCommand(t *testing.T) {
	proj := t.TempDir()
	writeRollout(t, proj, "aaaa1111", "/root/test", "caro game", time.Now())
	rig := newCodingRig(t, proj)
	s := rig.svc
	ctx := context.Background()
	chat := "9"

	s.handleTelegramCoding(ctx, "/resume", chat)
	if dm := rig.waitDM(t); !strings.Contains(dm, "/root/test") {
		t.Fatalf("/resume list DM = %q", dm)
	}
	s.handleTelegramCoding(ctx, "/resume 1", chat)
	if dm := rig.waitDM(t); !strings.Contains(dm, "In thread") {
		t.Fatalf("/resume 1 DM = %q", dm)
	}
	if tgt, ok := s.getCodingTarget(chat); !ok || tgt.SessionID != "aaaa1111" {
		t.Fatalf("/resume 1 did not select: %+v ok=%v", tgt, ok)
	}
}

func TestCodingSelectionPersists(t *testing.T) {
	proj := t.TempDir()
	writeRollout(t, proj, "th0", "/root/app", "app", time.Now())
	rig := newCodingRig(t, proj)
	rig.svc.setCodingTarget("9", codingTarget{Folder: "/root/app", SessionID: "th0"})

	s2 := &CodexService{codingSelPath: rig.svc.codingSelPath}
	tgt, ok := s2.getCodingTarget("9")
	if !ok || tgt.Folder != "/root/app" || tgt.SessionID != "th0" {
		t.Fatalf("persisted selection not recovered: %+v ok=%v", tgt, ok)
	}
}

func TestCodingLiveTUIGuard(t *testing.T) {
	rig := newCodingRig(t, t.TempDir())
	rig.svc.folderHasLiveCodex = func(string) bool { return true }
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

func TestParseCodexResult(t *testing.T) {
	ok := []byte(`{"type":"thread.started","thread_id":"th-1"}
{"type":"item.completed","item":{"type":"reasoning","text":"thinking"}}
{"type":"item.completed","item":{"type":"agent_message","text":"done"}}
{"type":"turn.completed","usage":{}}`)
	reply, id, terr := parseCodexResult(ok)
	if reply != "done" || id != "th-1" || terr != "" {
		t.Fatalf("ok: reply=%q id=%q terr=%q", reply, id, terr)
	}

	multi := []byte(`{"type":"thread.started","thread_id":"th-2"}
{"type":"item.completed","item":{"item_type":"agent_message","text":"part1"}}
{"type":"item.completed","item":{"item_type":"agent_message","text":"part2"}}
{"type":"turn.completed"}`)
	if reply, id, _ := parseCodexResult(multi); reply != "part1\n\npart2" || id != "th-2" {
		t.Fatalf("multi: reply=%q id=%q", reply, id)
	}

	failed := []byte(`{"type":"thread.started","thread_id":"th-3"}
{"type":"error","message":"404 boom"}`)
	if reply, id, terr := parseCodexResult(failed); reply != "" || id != "th-3" || terr != "404 boom" {
		t.Fatalf("failed: reply=%q id=%q terr=%q", reply, id, terr)
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
	if err := os.WriteFile(path, []byte("# c\nOPENAI_API_KEY = \"sk-1\"\nbad line\nX=y\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	pairs := loadEnvFilePairs(path)
	want := map[string]bool{"OPENAI_API_KEY=sk-1": true, "X=y": true}
	if len(pairs) != len(want) {
		t.Fatalf("pairs = %v", pairs)
	}
	for _, p := range pairs {
		if !want[p] {
			t.Errorf("unexpected pair %q", p)
		}
	}
}
