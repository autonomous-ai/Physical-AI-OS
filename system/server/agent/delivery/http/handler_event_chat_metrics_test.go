package http

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/monitor"
)

type finalMetricsGateway struct {
	domain.AgentGateway
	pending  bool
	releases int
}

func (g *finalMetricsGateway) Name() string { return "OpenClaw" }
func (g *finalMetricsGateway) RemovePendingChatTraceByRunID(string) bool {
	pending := g.pending
	g.pending = false
	return pending
}
func (g *finalMetricsGateway) SetBusy(busy bool) {
	if !busy {
		g.releases++
	}
}

// executionLogCapture observes the synchronous local telemetry record, independent of transport timing.
type executionLogCapture struct {
	slog.Handler
	events chan string
}

func (h executionLogCapture) Handle(ctx context.Context, record slog.Record) error {
	if record.Message == "[telemetry] event" {
		var name, params string
		record.Attrs(func(a slog.Attr) bool {
			if a.Key == "event_name" {
				name = a.Value.String()
			}
			if a.Key == "params" {
				params = a.Value.String()
			}
			return true
		})
		if name == "voice_metrics_task_execution" {
			h.events <- params
		}
	}
	return nil
}

func TestChatFinalWithoutLifecycleReportsOnlyProvenCompletion(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	t.Setenv("AUTONOMOUS_ANALYTICS_URL", server.URL)
	for _, tc := range []struct {
		name, runID, state, message string
		pending, completed          bool
		releases                    int
	}{
		{"slash-final", "device-chat-slash", "final", "Current status", true, true, 1},
		{"empty-final", "device-chat-empty", "final", "  ", true, false, 1},
		{"lifecycle-already-opened", "device-chat-started", "final", "Answer", false, false, 0},
		{"partial-is-not-completion", "device-chat-partial", "partial", "Thinking", true, false, 0},
		{"unrelated-run", "backend-uuid", "final", "Answer", true, false, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			observed := make(chan string, 8)
			original := slog.Default()
			slog.SetDefault(slog.New(executionLogCapture{
				Handler: slog.NewTextHandler(io.Discard, nil), events: observed,
			}))
			t.Cleanup(func() { slog.SetDefault(original) })
			gateway := &finalMetricsGateway{pending: tc.pending}
			h := &AgentHandler{agentGateway: gateway, monitorBus: monitor.ProvideBus()}
			payload, err := json.Marshal(map[string]string{
				"runId": tc.runID, "role": "assistant", "state": tc.state, "message": tc.message,
			})
			if err != nil {
				t.Fatal(err)
			}
			// A retried final must not count the task a second time.
			for range 2 {
				if err := h.handleChatEvent(domain.WSEvent{Payload: payload}); err != nil {
					t.Fatal(err)
				}
			}
			wantEvents := 0
			if tc.completed {
				wantEvents = 1
			}
			if len(observed) != wantEvents {
				t.Fatalf("execution events = %d, want %d", len(observed), wantEvents)
			}
			if tc.completed {
				var params map[string]any
				if err := json.Unmarshal([]byte(<-observed), &params); err != nil {
					t.Fatal(err)
				}
				if params["run_id"] != tc.runID || params["outcome"] != "completed" || params["evidence"] != "chat_final_no_lifecycle" {
					t.Fatalf("wrong completion ownership/evidence: %+v", params)
				}
				if _, exists := params["message"]; exists {
					t.Fatal("reply text must not enter telemetry")
				}
			}
			if gateway.releases != tc.releases {
				t.Fatalf("busy releases = %d, want %d", gateway.releases, tc.releases)
			}
		})
	}
}
