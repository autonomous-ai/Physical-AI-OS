package externalhistory

import (
	"errors"
	"strings"
	"testing"
)

type syncGateway struct {
	ready, busy bool
	marked      []string
	sent        []string
	messages    []string
	err         error
	onSend      func()
}

func (g *syncGateway) SetPendingChatTrace(string, string) {}
func (g *syncGateway) IsReady() bool                      { return g.ready }
func (g *syncGateway) IsBusy() bool                       { return g.busy }
func (g *syncGateway) MarkSilentRun(id string)            { g.marked = append(g.marked, id) }
func (g *syncGateway) SendChatMessageWithRun(message, reqID, runID string) (string, error) {
	if len(g.marked) == 0 || g.marked[len(g.marked)-1] != runID {
		panic("send before silent mark")
	}
	if reqID != runID {
		panic("unstable identity")
	}
	g.sent = append(g.sent, runID)
	g.messages = append(g.messages, message)
	if g.onSend != nil {
		g.onSend()
	}
	return runID, g.err
}
func pendingSync(t *testing.T, s *Store, source, id string) Record {
	t.Helper()
	r, err := s.Begin(Record{Source: source, OriginRunID: id, AgentID: "agent-a", AgentName: "Coder", Input: "Open Chrome"})
	if err != nil {
		t.Fatal(err)
	}
	if err = s.Complete(source, id, "A tab is open."); err != nil {
		t.Fatal(err)
	}
	return r
}
func TestSyncUsesRealtimeHistoryFormatAndSilentBeforeSend(t *testing.T) {
	s, err := New(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	r := pendingSync(t, s, "harness", "one")
	g := &syncGateway{}
	s.Flush(g)
	if len(g.sent) != 0 {
		t.Fatal("sent offline")
	}
	g.ready = true
	g.busy = true
	s.Flush(g)
	if len(g.sent) != 0 {
		t.Fatal("sent while busy")
	}
	g.busy = false
	s.Flush(g)
	if len(g.sent) != 1 || g.sent[0] != r.SyncRunID {
		t.Fatal(g.sent)
	}
	for _, text := range []string{"[skills: input-branching]", "[HANDLED]", "[REPLY]", "NO_REPLY", "harness", "Coder", "Open Chrome", "A tab is open."} {
		if !strings.Contains(g.messages[0], text) {
			t.Fatalf("missing %s", text)
		}
	}
	stored, _ := s.Lookup(r.SyncRunID)
	if stored.State != StateSending {
		t.Fatal("socket write treated as ack")
	}
	s.Flush(g)
	if len(g.sent) != 1 {
		t.Fatal("duplicate send before ack")
	}
	if err = s.Acknowledge(r.SyncRunID); err != nil {
		t.Fatal(err)
	}
	s.Flush(g)
	if len(g.sent) != 1 {
		t.Fatal("resent completed turn")
	}
}
func TestSyncRestartRestoresSilenceAndOnlySendsNeverAttemptedRecords(t *testing.T) {
	dir := t.TempDir()
	s, _ := New(dir)
	first := pendingSync(t, s, "harness", "one")
	g := &syncGateway{ready: true, err: errors.New("connection lost")}
	s.Flush(g)
	second := pendingSync(t, s, "another-device", "two")
	s, err := New(dir)
	if err != nil {
		t.Fatal(err)
	}
	g = &syncGateway{ready: true}
	s.RestoreSilent(g)
	if len(g.marked) != 1 || g.marked[0] != first.SyncRunID {
		t.Fatal("lost silent state", g.marked)
	}
	s.Flush(g)
	if len(g.sent) != 1 || g.sent[0] != second.SyncRunID {
		t.Fatal("ambiguous send replayed", g.sent)
	}
	if err = s.Acknowledge(first.SyncRunID); err != nil {
		t.Fatal(err)
	}
	r, _ := s.Lookup(first.SyncRunID)
	if r.State != StateDone {
		t.Fatal(r.State)
	}
}
func TestSyncAckBeforeSendReturnIsNotOverwrittenByError(t *testing.T) {
	s, _ := New(t.TempDir())
	r := pendingSync(t, s, "harness", "one")
	g := &syncGateway{ready: true, err: errors.New("late socket error"), onSend: func() {
		if err := s.Acknowledge(r.SyncRunID); err != nil {
			t.Fatal(err)
		}
	}}
	s.Flush(g)
	got, _ := s.Lookup(r.SyncRunID)
	if got.State != StateDone {
		t.Fatal(got.State)
	}
}

func TestMessageForbidsActionsFromHistory(t *testing.T) {
	m := Message(Record{Source: "realtime", Input: "Look at my camera", Output: "All done!"})
	for _, want := range []string{
		"Do not call device, camera, face, voice or messaging tools",
		"Do not enroll anyone or take names from this entry",
		"Do not record names or identities from this entry",
		"Return NO_REPLY.",
	} {
		if !strings.Contains(m, want) {
			t.Fatalf("message missing %q:\n%s", want, m)
		}
	}
}
