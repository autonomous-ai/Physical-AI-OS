package opencode

import (
	"encoding/json"
	"fmt"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

func TestQueuedFollowupsRetainTheirRunIDs(t *testing.T) {
	for _, tagged := range []bool{false, true} {
		t.Run(fmt.Sprintf("tagged=%v", tagged), func(t *testing.T) {
			s := &OpenCodeService{}
			for i := 1; i <= 3; i++ {
				s.addPendingRun(fmt.Sprintf("req-%d", i), fmt.Sprintf("run-%d", i))
			}
			for i := 1; i <= 3; i++ {
				var events []domain.WSEvent
				dispatch := func(e domain.WSEvent) { events = append(events, e) }
				for _, frame := range []map[string]any{
					{"type": "step_start"},
					{"type": "text", "text": fmt.Sprintf("reply-%d", i)},
					{"type": "session.idle"},
				} {
					if tagged {
						frame["request_id"] = fmt.Sprintf("req-%d", i)
						frame["run_id"] = fmt.Sprintf("run-%d", i)
					}
					raw, _ := json.Marshal(frame)
					s.translateFrame(raw, dispatch)
				}
				if len(events) != 4 {
					t.Fatalf("turn %d: events=%d", i, len(events))
				}
				for _, event := range events {
					var payload map[string]any
					_ = json.Unmarshal(event.Payload, &payload)
					if payload["runId"] != fmt.Sprintf("run-%d", i) {
						t.Fatalf("turn %d misattributed: %s", i, event.Payload)
					}
				}
			}
			if s.hasPendingRuns() {
				t.Fatal("pending queue leaked completed runs")
			}
		})
	}
}

func TestRejectedFollowupDoesNotEndCurrentTurnOrConsumeNext(t *testing.T) {
	s := &OpenCodeService{}
	for _, id := range []string{"first", "second", "third"} {
		s.addPendingRun(id, "run-"+id)
	}
	var events []domain.WSEvent
	dispatch := func(e domain.WSEvent) { events = append(events, e) }
	s.translateFrame([]byte(`{"type":"step_start","request_id":"first","run_id":"run-first"}`), dispatch)
	s.translateFrame([]byte(`{"type":"bridge.rejected","request_id":"third","run_id":"run-third","error":"queue full"}`), dispatch)
	if s.getCurrentRunID() != "run-first" {
		t.Fatal("rejection ended current turn")
	}
	if got := s.takePendingRun("second", "", false).runID; got != "run-second" {
		t.Fatalf("other queued turn lost: %q", got)
	}
	var rejected map[string]any
	_ = json.Unmarshal(events[len(events)-1].Payload, &rejected)
	if rejected["runId"] != "run-third" {
		t.Fatalf("rejection misattributed: %v", rejected)
	}
}

func TestTaggedLateTerminalCannotStartAnotherTurn(t *testing.T) {
	s := &OpenCodeService{}
	s.addPendingRun("first", "run-first")
	s.addPendingRun("second", "run-second")
	dispatch := func(domain.WSEvent) {}
	s.translateFrame([]byte(`{"type":"session.idle","request_id":"first","run_id":"run-first"}`), dispatch)
	count := 0
	s.translateFrame([]byte(`{"type":"session.idle","request_id":"first","run_id":"run-first"}`), func(domain.WSEvent) { count++ })
	if count != 0 || s.getCurrentRunID() != "" {
		t.Fatal("late terminal reopened completed run")
	}
	if got := s.takePendingRun("second", "", false).runID; got != "run-second" {
		t.Fatal("late frame consumed queued run")
	}
}

func TestLateOrInterleavedThreadCannotChangeSession(t *testing.T) {
	s := &OpenCodeService{}
	s.addPendingRun("first", "run-first")
	var startSession string
	s.translateFrame([]byte(`{"type":"step_start","sessionID":"correct","request_id":"first","run_id":"run-first"}`), func(e domain.WSEvent) {
		var payload map[string]any
		_ = json.Unmarshal(e.Payload, &payload)
		startSession, _ = payload["sessionKey"].(string)
	})
	if startSession != "correct" {
		t.Fatalf("start used wrong session: %q", startSession)
	}
	s.translateFrame([]byte(`{"type":"step_start","sessionID":"interleaved","request_id":"other","run_id":"run-other"}`), func(domain.WSEvent) { t.Fatal("interleaved frame dispatched") })
	if s.GetSessionKey() != "correct" {
		t.Fatal("interleaved frame changed session")
	}
	s.translateFrame([]byte(`{"type":"session.idle","request_id":"first","run_id":"run-first"}`), func(domain.WSEvent) {})
	s.translateFrame([]byte(`{"type":"step_start","sessionID":"stale","request_id":"first","run_id":"run-first"}`), func(domain.WSEvent) { t.Fatal("late frame dispatched") })
	if s.GetSessionKey() != "correct" {
		t.Fatal("late frame changed session")
	}
}

func TestQueuedRejectionBusyCallbackPreservesActiveUntilLastTurn(t *testing.T) {
	s := &OpenCodeService{}
	s.addPendingRun("first", "run-first")
	s.addPendingRun("second", "run-second")
	s.addPendingRun("rejected", "run-rejected")
	dispatch := func(e domain.WSEvent) {
		var payload struct {
			Data struct {
				Phase string `json:"phase"`
			} `json:"data"`
		}
		_ = json.Unmarshal(e.Payload, &payload)
		if payload.Data.Phase == "end" || payload.Data.Phase == "error" {
			s.SetBusy(false)
		}
	}
	s.translateFrame([]byte(`{"type":"step_start","request_id":"first","run_id":"run-first"}`), dispatch)
	s.translateFrame([]byte(`{"type":"bridge.rejected","request_id":"rejected","run_id":"run-rejected","error":"full"}`), dispatch)
	if !s.activeTurn.Load() || s.getCurrentRunID() != "run-first" {
		t.Fatal("rejection idled active turn")
	}
	s.translateFrame([]byte(`{"type":"session.idle","request_id":"first","run_id":"run-first"}`), dispatch)
	if !s.activeTurn.Load() {
		t.Fatal("queued accepted turn lost busy gating")
	}
	s.translateFrame([]byte(`{"type":"session.idle","request_id":"second","run_id":"run-second"}`), dispatch)
	if s.activeTurn.Load() {
		t.Fatal("last terminal did not release busy gating")
	}
}

func TestDisconnectedPendingRequestsCannotKeepBusyForever(t *testing.T) {
	s := &OpenCodeService{}
	s.addPendingRun("lost", "run-lost")
	s.setCurrentRunID("run-active")
	s.clearTurn()
	s.SetBusy(false)
	if s.activeTurn.Load() || s.hasPendingRuns() {
		t.Fatal("disconnect retained phantom busy requests")
	}
}

func TestReusedRequestIDWithDifferentRunSurvivesRestartOverlap(t *testing.T) {
	s := &OpenCodeService{}
	s.addPendingRun("chat-1", "new-run")
	s.translateFrame([]byte(`{"type":"session.idle","request_id":"chat-1","run_id":"old-run"}`), func(domain.WSEvent) {})
	var got string
	s.translateFrame([]byte(`{"type":"session.idle","request_id":"chat-1","run_id":"new-run"}`), func(e domain.WSEvent) {
		var payload map[string]any
		_ = json.Unmarshal(e.Payload, &payload)
		got, _ = payload["runId"].(string)
	})
	if got != "new-run" || s.hasPendingRuns() {
		t.Fatalf("new request suppressed by old ID: run=%q", got)
	}
}
