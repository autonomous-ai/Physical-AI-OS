package hermes

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// This opt-in test uses an isolated real Hermes API and its configured LLM.
// The endpoint file must contain {"url": "http://127.0.0.1:...", "key": "..."}.
func TestLocalHermesRunsIntegration(t *testing.T) {
	path := os.Getenv("HERMES_INTEGRATION_ENDPOINT_FILE")
	if path == "" {
		t.Skip("requires an isolated real Hermes API and LLM")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var endpoint struct {
		URL string `json:"url"`
		Key string `json:"key"`
	}
	if err := json.Unmarshal(raw, &endpoint); err != nil {
		t.Fatal(err)
	}
	oldURL, oldKey := BaseURL, APIKey
	BaseURL, APIKey = endpoint.URL, endpoint.Key
	t.Cleanup(func() { BaseURL, APIKey = oldURL, oldKey })
	s := &HermesService{httpClient: &http.Client{}}
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Minute)
	defer cancel()
	if !s.discoverRunSteering(ctx) {
		t.Fatal("real Hermes did not advertise enriched Runs capability")
	}
	for _, action := range []string{"steer", "stop"} {
		t.Run(action, func(t *testing.T) {
			prompt := `Integration test. Use terminal exactly once with timeout 20 seconds, command: python3 -c "import time; time.sleep(8); print('OS_TOOL_BEGIN' + 'x'*700 + 'OS_TOOL_END')". Wait for the result. Then reply with ORIGINAL_RESULT unless a later user instruction changes it. Do not access any other files or services.`
			id, err := s.createManagedRun(ctx, streamRequest{Input: prompt}, "")
			if err != nil {
				t.Fatal(err)
			}
			settled := false
			defer func() {
				if !settled {
					cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 5*time.Second)
					defer cleanupCancel()
					_ = s.controlManagedRun(cleanupCtx, id, "stop", "")
				}
			}()
			started := make(chan struct{}, 1)
			type completion struct {
				terminal, errored       bool
				final, pending          string
				err                     error
				starts, ends            int
				fullResult, correctArgs bool
			}
			done := make(chan completion, 1)
			go func() {
				observed := completion{}
				calls := map[string]bool{}
				result, pending, readErr := s.readManagedRun(ctx, id, "os-local-integration", func(event domain.WSEvent) {
					var payload struct {
						Stream string `json:"stream"`
						Data   struct {
							Phase      string          `json:"phase"`
							ToolCallID string          `json:"toolCallId"`
							Arguments  string          `json:"arguments"`
							Result     json.RawMessage `json:"result"`
						} `json:"data"`
					}
					if json.Unmarshal(event.Payload, &payload) != nil || payload.Stream != "tool" {
						return
					}
					if payload.Data.Phase == "start" {
						observed.starts++
						if payload.Data.ToolCallID != "" {
							calls[payload.Data.ToolCallID] = true
						}
						observed.correctArgs = observed.correctArgs || strings.Contains(payload.Data.Arguments, "OS_TOOL_BEGIN")
						select {
						case started <- struct{}{}:
						default:
						}
					}
					if payload.Data.Phase == "end" {
						observed.ends++
						output := string(payload.Data.Result)
						observed.fullResult = observed.fullResult || (calls[payload.Data.ToolCallID] && strings.Contains(output, "OS_TOOL_BEGIN") && strings.Contains(output, "OS_TOOL_END") && strings.Contains(output, strings.Repeat("x", 700)))
					}
				})
				observed.terminal, observed.errored, observed.final, observed.pending, observed.err = result.Terminal, result.Errored, result.FinalText, pending, readErr
				done <- observed
			}()
			select {
			case <-started:
			case early := <-done:
				t.Fatalf("run ended before real tool execution: %+v", early)
			case <-ctx.Done():
				t.Fatal(ctx.Err())
			}
			message := "Change the final answer: after the running tool completes reply with exactly STEER_CONFIRMED_731. This replaces ORIGINAL_RESULT."
			if err := s.controlManagedRun(ctx, id, action, message); err != nil {
				t.Fatal(err)
			}
			select {
			case got := <-done:
				settled = got.terminal
				if got.err != nil || !got.terminal {
					t.Fatalf("run did not settle: %+v", got)
				}
				if action == "steer" && (got.errored || !got.correctArgs || !got.fullResult || !strings.Contains(got.final, "STEER_CONFIRMED_731") || got.pending != "") {
					t.Fatalf("steer/evidence mismatch: %+v", got)
				}
				if action == "stop" && !got.errored {
					t.Fatalf("stopped run reported success: %+v", got)
				}
				t.Logf("real Hermes %s: terminal=%v errored=%v tool starts=%d ends=%d full result=%v final=%q", action, got.terminal, got.errored, got.starts, got.ends, got.fullResult, got.final)
			case <-ctx.Done():
				t.Fatal(ctx.Err())
			}
		})
	}
}
