package server

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf8"

	"go.autonomous.ai/os/system/externalhistory"
	"go.autonomous.ai/os/system/harness"
	agenthttp "go.autonomous.ai/os/system/server/agent/delivery/http"
)

func TestHarnessHistoryPersistsInputAndExactAttributedReply(t *testing.T) {
	dir := t.TempDir()
	store, err := externalhistory.New(dir)
	if err != nil {
		t.Fatal(err)
	}
	s := &Server{externalHistory: store, agentHandler: &agenthttp.AgentHandler{}}
	state := harness.VoiceModeState{AgentID: "coder", AgentName: "My Coder", MachineID: "computer"}
	if err = s.beginHarnessHistory("origin", "Open Chrome", state); err != nil {
		t.Fatal(err)
	}
	store, err = externalhistory.New(dir)
	if err != nil {
		t.Fatal(err)
	}
	s.externalHistory = store
	if len(store.Pending()) != 0 {
		t.Fatal("sent incomplete exchange")
	}
	s.registerHarnessReply("coder", "origin", true, false)
	s.deliverHarnessFinal("other-agent", "origin", "wrong answer")
	if len(store.Pending()) != 0 {
		t.Fatal("wrong agent supplied history")
	}
	s.deliverHarnessFinal("coder", "origin", "A new Chrome tab is open.")
	s.deliverHarnessFinal("coder", "origin", "duplicate callback")
	rows := store.Pending()
	if len(rows) != 1 {
		t.Fatal(rows)
	}
	r := rows[0]
	if r.Source != "harness" || r.AgentName != "My Coder" || r.MachineID != "computer" || r.Input != "Open Chrome" || r.Output != "A new Chrome tab is open." {
		t.Fatal(r)
	}
	store, err = externalhistory.New(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(store.Pending()) != 1 {
		t.Fatal("restart lost completed pending exchange")
	}
}

func TestHarnessHistoryWriteFailureKeepsReplyRouteForRetry(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "history")
	store, _ := externalhistory.New(dir)
	s := &Server{externalHistory: store, agentHandler: &agenthttp.AgentHandler{}}
	if err := s.beginHarnessHistory("origin", "question", harness.VoiceModeState{AgentID: "coder"}); err != nil {
		t.Fatal(err)
	}
	s.registerHarnessReply("coder", "origin", true, false)
	saved := dir + "-saved"
	if err := os.Rename(dir, saved); err != nil {
		t.Fatal(err)
	}
	s.deliverHarnessFinal("coder", "origin", "answer")
	if !s.hasHarnessReply("coder", "origin") {
		t.Fatal("discarded reply route despite persistence failure")
	}
	if err := os.Rename(saved, dir); err != nil {
		t.Fatal(err)
	}
	s.deliverHarnessFinal("coder", "origin", "answer")
	if s.hasHarnessReply("coder", "origin") || len(store.Pending()) != 1 {
		t.Fatal("retry failed")
	}
}

func TestHarnessHistoryBoundsReplyWithoutBreakingUTF8(t *testing.T) {
	store, _ := externalhistory.New(t.TempDir())
	s := &Server{externalHistory: store}
	if err := s.beginHarnessHistory("origin", "question", harness.VoiceModeState{}); err != nil {
		t.Fatal(err)
	}
	if err := s.completeHarnessHistory("origin", strings.Repeat("你好", externalhistory.MaxOutputBytes)); err != nil {
		t.Fatal(err)
	}
	r := store.Pending()[0]
	if len(r.Output) > externalhistory.MaxOutputBytes || !utf8.ValidString(r.Output) || !strings.HasSuffix(r.Output, "[truncated]") {
		t.Fatal("invalid bounded reply")
	}
}
