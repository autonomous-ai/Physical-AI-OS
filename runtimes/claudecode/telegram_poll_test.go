package claudecode

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

// Hermetic end-to-end test of the device-owned Telegram receive loop
// (telegram_poll.go): a fake getUpdates server returns one allowed private
// message plus two rejects (group chat, non-allowlisted sender); the loop must
// inject exactly one turn, mark the run trackers, upsert the accepted chat
// into the targets store, and persist the advanced offset — all under temp
// paths (no /root, no real Bot API, no WebSocket).
func TestTelegramPollLoop(t *testing.T) {
	dir := t.TempDir()

	var calls atomic.Int64
	var firstOffset atomic.Value // string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/botTESTTOKEN/sendChatAction" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"ok":true}`))
			return
		}
		if r.URL.Path != "/botTESTTOKEN/getUpdates" {
			t.Errorf("unexpected path %q", r.URL.Path)
		}
		n := calls.Add(1)
		w.Header().Set("Content-Type", "application/json")
		if n == 1 {
			firstOffset.Store(r.URL.Query().Get("offset"))
			_, _ = w.Write([]byte(`{"ok":true,"result":[
				{"update_id":100,"message":{"text":"hello lamp","chat":{"id":777,"type":"private"},"from":{"id":777}}},
				{"update_id":101,"message":{"text":"group spam","chat":{"id":-500,"type":"group"},"from":{"id":777}}},
				{"update_id":102,"message":{"text":"intruder","chat":{"id":888,"type":"private"},"from":{"id":888}}}
			]}`))
			return
		}
		_, _ = w.Write([]byte(`{"ok":true,"result":[]}`))
	}))
	defer srv.Close()

	type sent struct{ text, reqID, runID string }
	injected := make(chan sent, 4)
	s := &ClaudeCodeService{
		config:              &config.Config{TelegramBotToken: "TESTTOKEN", TelegramUserID: "777"},
		telegramRuns:        make(map[string]string),
		silentRuns:          make(map[string]bool),
		telegramAPIBase:     srv.URL,
		telegramOffsetPath:  filepath.Join(dir, "telegram_offset.json"),
		telegramTargetsPath: filepath.Join(dir, "telegram_targets.json"),
	}
	s.telegramSendTurn = func(text, reqID, runID string) error {
		injected <- sent{text: text, reqID: reqID, runID: runID}
		return nil
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		s.startTelegramPoll(ctx)
		close(done)
	}()

	var got sent
	select {
	case got = <-injected:
	case <-time.After(5 * time.Second):
		t.Fatal("no turn injected within 5s")
	}
	if got.text != "[telegram] Message from unknown [id:777]:\nhello lamp" {
		t.Errorf("injected text = %q, want %q", got.text, "[telegram] Message from unknown [id:777]:\nhello lamp")
	}

	deadline := time.Now().Add(5 * time.Second)
	for calls.Load() < 2 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if calls.Load() < 2 {
		t.Fatal("second getUpdates poll never happened")
	}
	cancel()
	<-done

	select {
	case extra := <-injected:
		t.Errorf("unexpected extra injection: %+v", extra)
	default:
	}

	if off, _ := firstOffset.Load().(string); off != "0" {
		t.Errorf("first poll offset = %q, want %q (no prior state file)", off, "0")
	}

	data, err := os.ReadFile(s.telegramOffsetPath)
	if err != nil {
		t.Fatalf("read offset file: %v", err)
	}
	var st telegramOffsetState
	if err := json.Unmarshal(data, &st); err != nil {
		t.Fatalf("parse offset file: %v", err)
	}
	if st.Offset != 103 {
		t.Errorf("persisted offset = %d, want 103", st.Offset)
	}

	targets, err := s.GetTelegramTargets()
	if err != nil {
		t.Fatalf("GetTelegramTargets: %v", err)
	}
	if len(targets) != 1 || targets[0].ChatID != "777" || targets[0].Type != "private" {
		t.Errorf("targets = %+v, want exactly chat 777 (private)", targets)
	}

	if chatID := s.consumeTelegramRun(got.runID); chatID != "777" {
		t.Errorf("consumeTelegramRun(%q) = %q, want %q", got.runID, chatID, "777")
	}
	if !s.IsSilentRun(got.runID) {
		t.Errorf("run %q not marked silent — Telegram replies must not hit TTS", got.runID)
	}
}
