package http

import (
	"testing"
	"time"
)

func TestAssistantTimingDoesNotConsumeOrResetStream(t *testing.T) {
	h := &AgentHandler{streamStats: make(map[string]*runStreamStats)}
	if !h.assistantFirstDeltaAt("run").IsZero() {
		t.Fatal("missing run acquired timing")
	}
	if h.recordAssistantDelta("run", "") {
		t.Fatal("empty delta became first token")
	}
	before := time.Now()
	if !h.recordAssistantDelta("run", "Hello ") {
		t.Fatal("first delta not recorded")
	}
	first := h.assistantFirstDeltaAt("run")
	if first.Before(before) || first.After(time.Now()) {
		t.Fatal("invalid first delta time")
	}
	if h.recordAssistantDelta("run", "world.") {
		t.Fatal("second delta became first token")
	}
	if h.assistantFirstDeltaAt("run") != first {
		t.Fatal("timing reset")
	}
	stats := h.drainStreamStats("run")
	if stats.assistantText.String() != "Hello world." || stats.assistantChunks != 2 || stats.assistantFirstAt != first {
		t.Fatal("timing changed stream state")
	}
	if !h.assistantFirstDeltaAt("run").IsZero() {
		t.Fatal("drained timing leaked")
	}
}

func TestTTSTextKeyMatchesHALSHA256Prefix(t *testing.T) {
	if got := ttsTextKey("hello"); got != "2cf24dba5fb0" {
		t.Fatalf("hash = %s", got)
	}
	if ttsTextKey("hello ") == ttsTextKey("hello") {
		t.Fatal("timing hash altered text")
	}
}

func TestAssistantFinalTimingSurvivesDrainAndOmitsMissingDelta(t *testing.T) {
	first := time.Now()
	stats := &runStreamStats{assistantFirstAt: first}
	h := &AgentHandler{streamStats: map[string]*runStreamStats{"run": stats}}
	drained := h.drainStreamStats("run")
	if ms, known := drained.assistantElapsedMs(first.Add(2500 * time.Millisecond)); !known || ms != 2500 {
		t.Fatalf("final duration = %d, known = %v", ms, known)
	}
	for _, absent := range []*runStreamStats{nil, {}} {
		if _, known := absent.assistantElapsedMs(first); known {
			t.Fatal("invented timing without delta")
		}
	}
	if _, known := drained.assistantElapsedMs(first.Add(-time.Second)); known {
		t.Fatal("negative elapsed timing")
	}
}
