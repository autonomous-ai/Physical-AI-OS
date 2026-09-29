package codex

import (
	"reflect"
	"testing"
)

func TestObservationLossIncludesOnlyUnfinishedTransmittedTasks(t *testing.T) {
	s := &CodexService{}
	s.addPendingRun("unsent-request", "unsent")
	s.addPendingRun("queued-request", "queued")
	s.markPendingRunSent("queued-request")
	s.setCurrentRunID("active")
	want := []string{"active", "queued"}
	if got := s.unfinishedTaskRunIDs(); !reflect.DeepEqual(got, want) {
		t.Fatalf("unfinished evidence = %v, want %v", got, want)
	}
	s.finishCurrentCorrelation()
	if got := s.unfinishedTaskRunIDs(); !reflect.DeepEqual(got, []string{"queued"}) {
		t.Fatalf("completed run remained unfinished: %v", got)
	}
	s.removePendingRun("queued-request")
	if got := s.unfinishedTaskRunIDs(); len(got) != 0 {
		t.Fatalf("unsent request entered observation loss: %v", got)
	}
	// Fast terminal events can consume a request before its successful write returns.
	// Marking it sent afterward must never recreate finished work.
	s.markPendingRunSent("queued-request")
	if got := s.unfinishedTaskRunIDs(); len(got) != 0 {
		t.Fatalf("late write recreated terminal: %v", got)
	}
}

func TestSteeredObservationIsRetainedUntilTerminal(t *testing.T) {
	s := &CodexService{}
	s.steeredRuns = []pendingRun{{reqID: "steer", runID: "merged"}}
	if got := s.unfinishedTaskRunIDs(); !reflect.DeepEqual(got, []string{"merged"}) {
		t.Fatalf("merged task lost: %v", got)
	}
	s.takeSteeredRuns()
	if got := s.unfinishedTaskRunIDs(); len(got) != 0 {
		t.Fatalf("terminal merged task retained: %v", got)
	}
}
