package server

import (
	"context"
	"encoding/json"
	"errors"
	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/server/config"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"go.autonomous.ai/os/system/harness"
	agenthttp "go.autonomous.ai/os/system/server/agent/delivery/http"
)

type harnessMetricRecord struct {
	name   string
	params map[string]any
}

type harnessMetricCapture struct {
	slog.Handler
	mu   sync.Mutex
	rows []harnessMetricRecord
}

func (h *harnessMetricCapture) Handle(_ context.Context, r slog.Record) error {
	if r.Message != "[telemetry] event" {
		return nil
	}
	var row harnessMetricRecord
	r.Attrs(func(a slog.Attr) bool {
		if a.Key == "event_name" {
			row.name = a.Value.String()
		}
		if a.Key == "params" {
			_ = json.Unmarshal([]byte(a.Value.String()), &row.params)
		}
		return true
	})
	h.mu.Lock()
	h.rows = append(h.rows, row)
	h.mu.Unlock()
	return nil
}

func captureHarnessMetrics(t *testing.T) *harnessMetricCapture {
	t.Helper()
	uplink := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(204) }))
	t.Cleanup(uplink.Close)
	t.Setenv("AUTONOMOUS_ANALYTICS_URL", uplink.URL)
	capture := &harnessMetricCapture{Handler: slog.NewTextHandler(io.Discard, nil)}
	previous := slog.Default()
	slog.SetDefault(slog.New(capture))
	t.Cleanup(func() { slog.SetDefault(previous) })
	return capture
}

func (h *harnessMetricCapture) executions() []map[string]any {
	h.mu.Lock()
	defer h.mu.Unlock()
	var result []map[string]any
	for _, row := range h.rows {
		if row.name == "voice_metrics_task_execution" && row.params["evidence"] != "harness_delegated" {
			result = append(result, row.params)
		}
	}
	return result
}

func TestHarnessExecutionRequiresResultNotBareLifecycle(t *testing.T) {
	for _, tc := range []struct {
		kind              string
		payload           map[string]any
		outcome, evidence string
	}{
		{"turn.done", nil, "", ""},
		{"turn.summary", map[string]any{"fullText": "result"}, "completed", "harness_turn_summary"},
		{"turn.summary", map[string]any{}, "", ""},
		{"turn.error", nil, "failed", "harness_turn_error"},
		{"agent.error", nil, "failed", "harness_turn_error"},
		{"question.open", map[string]any{}, "unknown", "harness_question_open"},
		{"turn.started", map[string]any{}, "", ""},
	} {
		t.Run(tc.kind+tc.outcome, func(t *testing.T) {
			capture := captureHarnessMetrics(t)
			s := &Server{agentHandler: &agenthttp.AgentHandler{}}
			s.registerHarnessReply("agent", "device-harness-test", true, false)
			frame := harness.Frame{"kind": tc.kind, "agentId": "agent", "runId": "device-harness-test"}
			if tc.payload != nil {
				frame["payload"] = tc.payload
			}
			s.forwardHarnessEvent(frame)
			rows := capture.executions()
			if tc.outcome == "" {
				if len(rows) != 0 {
					t.Fatalf("nonterminal produced execution: %v", rows)
				}
				return
			}
			if len(rows) != 1 || rows[0]["outcome"] != tc.outcome || rows[0]["evidence"] != tc.evidence || rows[0]["run_id"] != "device-harness-test" {
				t.Fatalf("wrong execution: %v", rows)
			}
			if tc.kind == "turn.done" && !s.hasHarnessReply("agent", "device-harness-test") {
				t.Fatal("metric consumed pending recap delivery")
			}
		})
	}
}

func TestHarnessMetricRejectsAmbiguousOrMismatchedRoute(t *testing.T) {
	for _, tc := range []struct {
		name, eventRun  string
		second, expired bool
		want            int
	}{
		{"single-legacy", "", false, false, 1},
		{"ambiguous-legacy", "", true, false, 0},
		{"explicit-match", "first", true, false, 1},
		{"explicit-mismatch", "different", false, false, 0},
		{"expired", "first", false, true, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			capture := captureHarnessMetrics(t)
			s := &Server{agentHandler: &agenthttp.AgentHandler{}}
			s.registerHarnessReply("agent", "first", true, false)
			if tc.second {
				s.registerHarnessReply("agent", "second", true, false)
			}
			if tc.expired {
				r := s.harnessReplies["first"]
				r.created = time.Now().Add(-16 * time.Minute)
				s.harnessReplies["first"] = r
			}
			s.observeHarnessExecution("agent", "turn.summary", harness.Frame{"runId": tc.eventRun, "payload": map[string]any{"fullText": "result"}})
			if rows := capture.executions(); len(rows) != tc.want {
				t.Fatalf("executions: %v", rows)
			}
			if !s.hasHarnessReply("agent", "first") {
				t.Fatal("measurement changed routing")
			}
		})
	}
}

func TestHarnessDispatchUnknownAndLocalQuestionAreNotSuccess(t *testing.T) {
	capture := captureHarnessMetrics(t)
	reportHarnessDispatchError("failed", "i1", errors.New("offline"))
	reportHarnessDispatchError("uncertain", "i2", &harness.DeliveryUnknownError{})
	reportHarnessDispatchError("accepted", "i3", nil)
	s := &Server{agentHandler: &agenthttp.AgentHandler{}}
	s.registerHarnessReply("agent", "question", true, false)
	s.deliverHarnessVoiceQuestion("agent", "question", "Next question?")
	rows := capture.executions()
	if len(rows) != 3 || rows[0]["outcome"] != "failed" || rows[1]["outcome"] != "unknown" || rows[2]["evidence"] != "harness_question_open" {
		t.Fatalf("incorrect dispatch/question outcomes: %v", rows)
	}
}

// Return a terminal synchronously, before the answer API has returned its receipt.
type harnessAnswerMetricTransport struct {
	voiceRouteTransport
	server *Server
	runID  string
}

func (f *harnessAnswerMetricTransport) Request(ctx context.Context, frame harness.Frame) (harness.Frame, error) {
	switch frame["type"] {
	case "status":
		return harness.Frame{"openQuestion": map[string]any{"requestId": "q1", "questions": []any{map[string]any{"key": "choice", "q": "Choose"}}}}, nil
	case "question.answer":
		f.server.forwardHarnessEvent(harness.Frame{"agentId": "mike", "runId": f.runID, "kind": "turn.summary", "payload": map[string]any{"fullText": "answer result"}})
		return harness.Frame{"receipt": map[string]any{"state": "queued"}}, nil
	default:
		return f.voiceRouteTransport.Request(ctx, frame)
	}
}
func TestHarnessAnswerAPIStartsChatBeforeImmediateTerminal(t *testing.T) {
	capture := captureHarnessMetrics(t)
	s := &Server{config: &config.Config{LLMAPIKey: "owner"}, agentHandler: &agenthttp.AgentHandler{}}
	transport := &harnessAnswerMetricTransport{server: s}
	s.harnessVoice = harness.NewVoiceController(transport, harness.VoiceCallbacks{SupportsMode: func(context.Context) (bool, error) { return true, nil },
		OnDispatch: func(agentID, runID string) {
			transport.runID = runID
			s.registerHarnessReply(agentID, runID, true, false)
		},
	})
	if err := s.harnessVoice.RefreshFocus(context.Background()); err != nil {
		t.Fatal(err)
	}
	if _, err := s.harnessVoice.SetMode(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	router := gin.New()
	s.registerHarnessVoiceRoutes(router.Group("/api/harness"))
	req := httptest.NewRequest("POST", "/api/harness/voice-mode/answer", strings.NewReader(`{"questionRequestId":"q1","focusRevision":"test:1","answers":{"choice":"yes"}}`))
	req.Header.Set("Authorization", "Bearer owner")
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	router.ServeHTTP(w, req)
	if w.Code != 200 {
		t.Fatalf("answer failed: %d %s", w.Code, w.Body.String())
	}
	capture.mu.Lock()
	defer capture.mu.Unlock()
	startIndex, doneIndex := -1, -1
	for i, row := range capture.rows {
		if row.name == "voice_metrics_task_started" {
			t.Fatal("typed answer counted as voice")
		}
		if row.name == "chat_metrics_task_started" {
			startIndex = i
			if row.params["run_id"] != transport.runID {
				t.Fatal("start run mismatch")
			}
		}
		if row.params["evidence"] == "harness_turn_summary" {
			doneIndex = i
			if row.params["run_id"] != transport.runID {
				t.Fatal("terminal run mismatch")
			}
		}
	}
	if startIndex < 0 || doneIndex <= startIndex {
		t.Fatalf("start/terminal order = %d/%d", startIndex, doneIndex)
	}
}

func TestHarnessReceiptIsNotExecutionCompletion(t *testing.T) {
	capture := captureHarnessMetrics(t)
	for _, state := range []string{"queued", "delivered", "started", "completed", "rejected"} {
		reportHarnessReceipt(state, harness.Frame{"receipt": map[string]any{"state": state}})
	}
	rows := capture.executions()
	if len(rows) != 1 || rows[0]["run_id"] != "rejected" || rows[0]["outcome"] != "failed" {
		t.Fatalf("receipts inferred execution completion: %v", rows)
	}
}
