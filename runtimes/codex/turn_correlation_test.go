package codex

import (
	"encoding/json"
	"fmt"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

func TestQueuedFollowupsRetainTheirRunIDs(t *testing.T) {
	for _, tagged := range []bool{false, true} {
		t.Run(fmt.Sprintf("tagged=%v", tagged), func(t *testing.T) {
			s := &CodexService{}
			for i := 1; i <= 3; i++ {
				s.addPendingRun(fmt.Sprintf("req-%d", i), fmt.Sprintf("run-%d", i))
			}
			for i := 1; i <= 3; i++ {
				var events []domain.WSEvent
				dispatch := func(e domain.WSEvent) { events = append(events, e) }
				for _, frame := range []map[string]any{
					{"type": "turn.started"},
					{"type": "item.completed", "item": map[string]any{"type": "agent_message", "text": fmt.Sprintf("reply-%d", i)}},
					{"type": "turn.completed"},
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
	s := &CodexService{}
	for _, id := range []string{"first", "second", "third"} {
		s.addPendingRun(id, "run-"+id)
	}
	var events []domain.WSEvent
	dispatch := func(e domain.WSEvent) { events = append(events, e) }
	s.translateFrame([]byte(`{"type":"turn.started","request_id":"first","run_id":"run-first"}`), dispatch)
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

func TestSteeredSilentHistoryRetiresItsPendingRunWithoutEndingActiveTurn(t *testing.T) {
	s := &CodexService{}
	s.addPendingRun("first", "run-first")
	s.addPendingRun("steered", "run-steered")
	s.MarkSilentRun("run-steered")
	var events []domain.WSEvent
	dispatch := func(e domain.WSEvent) {
		events = append(events, e)
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
	s.translateFrame([]byte(`{"type":"turn.started","request_id":"first","run_id":"run-first"}`), dispatch)
	s.translateFrame([]byte(`{"type":"bridge.steered","request_id":"steered","run_id":"run-steered"}`), dispatch)
	if s.getCurrentRunID() != "run-first" {
		t.Fatalf("steered acknowledgement ended active run: %q", s.getCurrentRunID())
	}
	if s.hasPendingRuns() {
		t.Fatal("steered request leaked in pending runs")
	}
	var terminal struct {
		RunID string `json:"runId"`
		Data  struct {
			Phase string `json:"phase"`
		} `json:"data"`
	}
	if err := json.Unmarshal(events[len(events)-1].Payload, &terminal); err != nil {
		t.Fatal(err)
	}
	if terminal.RunID != "run-steered" || terminal.Data.Phase != "end" {
		t.Fatalf("steered trace did not terminate cleanly: %s", events[len(events)-1].Payload)
	}
	s.translateFrame([]byte(`{"type":"turn.completed","request_id":"first","run_id":"run-first"}`), dispatch)
	if s.activeTurn.Load() {
		t.Fatal("completed active turn remained busy after steered follow-up")
	}
}

func TestSteeredWebFollowupReceivesTheSharedFinalReply(t *testing.T) {
	s := &CodexService{webChatRuns: make(map[string]bool)}
	s.addPendingRun("first", "run-first")
	s.addPendingRun("web", "run-web")
	s.MarkWebChatRun("run-web")
	var events []domain.WSEvent
	dispatch := func(e domain.WSEvent) { events = append(events, e) }
	s.translateFrame([]byte(`{"type":"turn.started","request_id":"first","run_id":"run-first"}`), dispatch)
	s.translateFrame([]byte(`{"type":"bridge.steered","request_id":"web","run_id":"run-web"}`), dispatch)
	if s.hasPendingRuns() || len(s.steeredRuns) != 1 {
		t.Fatalf("web follow-up was not moved out of pending: pending=%v steered=%v", s.pendingRuns, s.steeredRuns)
	}
	if len(events) != 2 {
		t.Fatalf("web follow-up should start and await the shared reply: %v", events)
	}
	s.translateFrame([]byte(`{"type":"item.completed","request_id":"first","run_id":"run-first","item":{"type":"agent_message","text":"[HW:/audio/play:{}] shared reply"}}`), dispatch)
	s.translateFrame([]byte(`{"type":"turn.completed","request_id":"first","run_id":"run-first"}`), dispatch)

	var gotReply, gotEnd bool
	for _, event := range events {
		var payload struct {
			RunID  string `json:"runId"`
			Stream string `json:"stream"`
			Data   struct {
				Delta string `json:"delta"`
				Phase string `json:"phase"`
			} `json:"data"`
		}
		_ = json.Unmarshal(event.Payload, &payload)
		if payload.RunID != "run-web" {
			continue
		}
		if payload.Stream == "assistant" {
			gotReply = payload.Data.Delta == "shared reply"
		}
		if payload.Stream == "lifecycle" && payload.Data.Phase == "end" {
			gotEnd = true
		}
	}
	if !gotReply || !gotEnd {
		t.Fatalf("web follow-up did not receive a safe shared final reply: %v", events)
	}
}

func TestTaggedLateTerminalCannotStartAnotherTurn(t *testing.T) {
	s := &CodexService{}
	s.addPendingRun("first", "run-first")
	s.addPendingRun("second", "run-second")
	dispatch := func(domain.WSEvent) {}
	s.translateFrame([]byte(`{"type":"turn.completed","request_id":"first","run_id":"run-first"}`), dispatch)
	count := 0
	s.translateFrame([]byte(`{"type":"turn.completed","request_id":"first","run_id":"run-first"}`), func(domain.WSEvent) { count++ })
	if count != 0 || s.getCurrentRunID() != "" {
		t.Fatal("late terminal reopened completed run")
	}
	if got := s.takePendingRun("second", "", false).runID; got != "run-second" {
		t.Fatal("late frame consumed queued run")
	}
}

func TestLateOrInterleavedThreadCannotChangeSession(t *testing.T) {
	s := &CodexService{}
	s.addPendingRun("first", "run-first")
	var startSession string
	s.translateFrame([]byte(`{"type":"thread.started","thread_id":"correct","request_id":"first","run_id":"run-first"}`), func(e domain.WSEvent) {
		var payload map[string]any
		_ = json.Unmarshal(e.Payload, &payload)
		startSession, _ = payload["sessionKey"].(string)
	})
	if startSession != "correct" {
		t.Fatalf("start used wrong session: %q", startSession)
	}
	s.translateFrame([]byte(`{"type":"thread.started","thread_id":"interleaved","request_id":"other","run_id":"run-other"}`), func(domain.WSEvent) { t.Fatal("interleaved frame dispatched") })
	if s.GetSessionKey() != "correct" {
		t.Fatal("interleaved frame changed session")
	}
	s.translateFrame([]byte(`{"type":"turn.completed","request_id":"first","run_id":"run-first"}`), func(domain.WSEvent) {})
	s.translateFrame([]byte(`{"type":"thread.started","thread_id":"stale","request_id":"first","run_id":"run-first"}`), func(domain.WSEvent) { t.Fatal("late frame dispatched") })
	if s.GetSessionKey() != "correct" {
		t.Fatal("late frame changed session")
	}
}

func TestQueuedRejectionBusyCallbackPreservesActiveUntilLastTurn(t *testing.T) {
	s := &CodexService{}
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
	s.translateFrame([]byte(`{"type":"turn.started","request_id":"first","run_id":"run-first"}`), dispatch)
	s.translateFrame([]byte(`{"type":"bridge.rejected","request_id":"rejected","run_id":"run-rejected","error":"full"}`), dispatch)
	if !s.activeTurn.Load() || s.getCurrentRunID() != "run-first" {
		t.Fatal("rejection idled active turn")
	}
	s.translateFrame([]byte(`{"type":"turn.completed","request_id":"first","run_id":"run-first"}`), dispatch)
	if !s.activeTurn.Load() {
		t.Fatal("queued accepted turn lost busy gating")
	}
	s.translateFrame([]byte(`{"type":"turn.completed","request_id":"second","run_id":"run-second"}`), dispatch)
	if s.activeTurn.Load() {
		t.Fatal("last terminal did not release busy gating")
	}
}

func TestDisconnectedPendingRequestsCannotKeepBusyForever(t *testing.T) {
	s := &CodexService{}
	s.addPendingRun("lost", "run-lost")
	s.setCurrentRunID("run-active")
	s.clearTurn()
	s.SetBusy(false)
	if s.activeTurn.Load() || s.hasPendingRuns() {
		t.Fatal("disconnect retained phantom busy requests")
	}
}

func TestGatewayDisconnectEndsActiveTurnBeforeCleanup(t *testing.T) {
	s := &CodexService{}
	s.setCurrentRunID("run-active")
	s.currentRequestID.Store("request-active")
	var got struct {
		RunID string `json:"runId"`
		Data  struct {
			Phase string `json:"phase"`
			Error string `json:"error"`
		} `json:"data"`
	}
	s.failDisconnectedTurn(func(event domain.WSEvent) {
		if err := json.Unmarshal(event.Payload, &got); err != nil {
			t.Fatal(err)
		}
	})
	if got.RunID != "run-active" || got.Data.Phase != "error" || got.Data.Error == "" {
		t.Fatalf("disconnect did not emit a correlated terminal error: %+v", got)
	}
	if s.getCurrentRunID() != "" {
		t.Fatalf("disconnect retained active run: %q", s.getCurrentRunID())
	}
}

func TestReusedRequestIDWithDifferentRunSurvivesRestartOverlap(t *testing.T) {
	s := &CodexService{}
	s.addPendingRun("chat-1", "new-run")
	s.translateFrame([]byte(`{"type":"turn.completed","request_id":"chat-1","run_id":"old-run"}`), func(domain.WSEvent) {})
	var got string
	s.translateFrame([]byte(`{"type":"turn.completed","request_id":"chat-1","run_id":"new-run"}`), func(e domain.WSEvent) {
		var payload map[string]any
		_ = json.Unmarshal(e.Payload, &payload)
		got, _ = payload["runId"].(string)
	})
	if got != "new-run" || s.hasPendingRuns() {
		t.Fatalf("new request suppressed by old ID: run=%q", got)
	}
}

func TestSteeredVoiceWaitsAndLatestFollowupOwnsSpeech(t *testing.T) {
	s := &CodexService{}
	var events []domain.WSEvent
	dispatch := func(e domain.WSEvent) { events = append(events, e) }
	s.addPendingRun("host", "host")
	s.translateFrame([]byte(`{"type":"turn.started","request_id":"host","run_id":"host"}`), dispatch)
	for _, id := range []string{"voice-1", "voice-2"} {
		s.addPendingRun(id, id)
		s.translateFrame([]byte(fmt.Sprintf(`{"type":"bridge.steered","request_id":%q,"run_id":%q}`, id, id)), dispatch)
	}
	if len(s.steeredRuns) != 2 || s.getCurrentRunID() != "host" {
		t.Fatal("steered voices must await the host result")
	}
	for _, e := range events {
		var p struct{ Data struct{ Phase string } }
		_ = json.Unmarshal(e.Payload, &p)
		if p.Data.Phase != "start" {
			t.Fatalf("early completion: %s", e.Payload)
		}
	}
	s.translateFrame([]byte(`{"type":"item.completed","request_id":"host","run_id":"host","item":{"type":"agent_message","text":"[HW:/servo/move:{}] Done."}}`), dispatch)
	s.translateFrame([]byte(`{"type":"turn.completed","request_id":"host","run_id":"host"}`), dispatch)
	if !s.IsSilentRun("host") || !s.IsSilentRun("voice-1") || s.IsSilentRun("voice-2") {
		t.Fatal("latest voice must own speech, suppressing host and earlier follow-up")
	}
	replies := map[string]string{}
	ends := map[string]int{}
	for _, e := range events {
		var p struct {
			RunID  string `json:"runId"`
			Stream string
			Data   struct {
				Delta  string
				Phase  string
				Merged bool `json:"mergedIntoActiveTurn"`
			}
		}
		_ = json.Unmarshal(e.Payload, &p)
		if p.Stream == "assistant" {
			replies[p.RunID] = p.Data.Delta
		}
		if p.Data.Phase == "end" {
			ends[p.RunID]++
			if p.RunID != "host" && !p.Data.Merged {
				t.Fatal("missing merged lifecycle marker")
			}
		}
	}
	if replies["host"] != "[HW:/servo/move:{}] Done." || replies["voice-1"] != "Done." || replies["voice-2"] != "Done." {
		t.Fatalf("shared reply must preserve host hardware once and strip child hardware: %v", replies)
	}
	for _, id := range []string{"host", "voice-1", "voice-2"} {
		if ends[id] != 1 {
			t.Fatalf("terminal count for %s: %d", id, ends[id])
		}
	}
	if len(s.steeredRuns) != 0 {
		t.Fatal("merged requests leaked")
	}
}

func TestSteeredFollowupsEndOnFailureDisconnectAndTimeout(t *testing.T) {
	for _, terminal := range []string{"failure", "disconnect", "timeout"} {
		t.Run(terminal, func(t *testing.T) {
			s := &CodexService{}
			ends := map[string]int{}
			dispatch := func(e domain.WSEvent) {
				var p struct {
					RunID string `json:"runId"`
					Data  struct {
						Phase string
						Error string
					}
				}
				_ = json.Unmarshal(e.Payload, &p)
				if p.Data.Phase == "error" && p.Data.Error != "" {
					ends[p.RunID]++
				}
			}
			s.addPendingRun("host", "host")
			s.translateFrame([]byte(`{"type":"turn.started","request_id":"host","run_id":"host"}`), dispatch)
			s.addPendingRun("voice", "voice")
			s.translateFrame([]byte(`{"type":"bridge.steered","request_id":"voice","run_id":"voice"}`), dispatch)
			switch terminal {
			case "failure":
				s.translateFrame([]byte(`{"type":"turn.failed","request_id":"host","run_id":"host","error":{"message":"failed"}}`), dispatch)
			case "disconnect":
				s.failDisconnectedTurn(dispatch)
			case "timeout":
				s.wsDispatch.Store(dispatchFn(dispatch))
				s.failStuckTurn()
			}
			if ends["host"] != 1 || ends["voice"] != 1 || len(s.steeredRuns) != 0 {
				t.Fatalf("terminal cleanup failed: %v, retained=%v", ends, s.steeredRuns)
			}
		})
	}
}

func TestSteeredVoiceAfterWebHostKeepsVoiceAndWebRoutes(t *testing.T) {
	s := &CodexService{webChatRuns: make(map[string]bool)}
	s.MarkWebChatRun("host")
	s.addPendingRun("host", "host")
	dispatch := func(domain.WSEvent) {}
	s.translateFrame([]byte(`{"type":"turn.started","request_id":"host","run_id":"host"}`), dispatch)
	s.addPendingRun("voice", "voice")
	s.translateFrame([]byte(`{"type":"bridge.steered","request_id":"voice","run_id":"voice"}`), dispatch)
	s.translateFrame([]byte(`{"type":"turn.completed","request_id":"host","run_id":"host"}`), dispatch)
	if !s.IsWebChatRun("host") || s.IsSilentRun("host") || s.IsSilentRun("voice") {
		t.Fatal("web host must retain its web marker, voice must remain audible")
	}
}
