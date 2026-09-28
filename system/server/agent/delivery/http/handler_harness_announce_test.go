package http

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

type halAnnounceTransport func(*http.Request) (*http.Response, error)

func (f halAnnounceTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// captureHALUpdates records every HAL request body by path; HAL's client uses
// http.DefaultTransport.
func captureHALUpdates(t *testing.T) <-chan map[string]any {
	t.Helper()
	original := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = original })
	got := make(chan map[string]any, 8)
	http.DefaultTransport = halAnnounceTransport(func(r *http.Request) (*http.Response, error) {
		payload := map[string]any{"_path": r.URL.Path}
		if r.Body != nil {
			_ = json.NewDecoder(r.Body).Decode(&payload)
			payload["_path"] = r.URL.Path
		}
		got <- payload
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{"status":"queued"}`))}, nil
	})
	return got
}

func nextHALUpdate(t *testing.T, got <-chan map[string]any) map[string]any {
	t.Helper()
	for {
		select {
		case payload := <-got:
			if payload["_path"] == "/voice/harness/update" {
				return payload
			}
		case <-time.After(2 * time.Second):
			t.Fatal("no Harness update reached HAL")
		}
	}
}

func TestHarnessResultIsQueuedWithAnnouncerNotSpokenRaw(t *testing.T) {
	got := captureHALUpdates(t)
	h := &AgentHandler{}
	h.MarkHarnessResponseRun("run-a", false, false)
	if !h.DeliverHarnessResponse("run-a", "**Built** the model.") {
		t.Fatal("result not delivered")
	}
	payload := nextHALUpdate(t, got)
	if payload["kind"] != "result" || payload["text"] != "**Built** the model." || payload["turn_id"] != "run-a" {
		t.Fatalf("wrong update %v", payload)
	}
}

func TestHarnessQuestionIsQueuedAsQuestion(t *testing.T) {
	got := captureHALUpdates(t)
	h := &AgentHandler{}
	h.MarkHarnessResponseRun("run-a", false, false)
	if !h.DeliverHarnessQuestion("run-a", "q1", "Which size?\n24\n27") {
		t.Fatal("question not delivered")
	}
	if payload := nextHALUpdate(t, got); payload["kind"] != "question" || payload["turn_id"] != "run-a" {
		t.Fatalf("wrong update %v", payload)
	}
}

func TestHarnessGroupedResultCarriesOutcome(t *testing.T) {
	got := captureHALUpdates(t)
	h := &AgentHandler{}
	h.MarkHarnessResponseRun("run-a", false, false)
	if err := h.SpeakHarnessGroupedResult("Done.", "failed", []string{"run-a"}); err != nil {
		t.Fatal(err)
	}
	if payload := nextHALUpdate(t, got); payload["kind"] != "result" || payload["outcome"] != "failed" {
		t.Fatalf("wrong update %v", payload)
	}
}

func TestHarnessProgressAnnouncedOnlyForLiveVoiceRuns(t *testing.T) {
	got := captureHALUpdates(t)
	h := &AgentHandler{}
	h.MarkHarnessResponseRun("voice", false, false)
	h.AnnounceHarnessProgress("voice", "Harness agent is working.")
	if payload := nextHALUpdate(t, got); payload["kind"] != "progress" || payload["turn_id"] != "voice" {
		t.Fatalf("wrong update %v", payload)
	}

	h.MarkHarnessResponseRun("web", true, false)
	h.MarkHarnessResponseRun("done", false, false)
	h.DeliverHarnessResponse("done", "Result")
	nextHALUpdate(t, got) // the result itself
	h.AnnounceHarnessProgress("web", "working")
	h.AnnounceHarnessProgress("done", "working")
	h.AnnounceHarnessProgress("unknown", "working")
	h.MarkHarnessResponseRun("cancelled", false, false)
	h.speechWatermarkMs.Store(time.Now().UnixMilli() + 1)
	h.AnnounceHarnessProgress("cancelled", "working")
	select {
	case payload := <-got:
		t.Fatalf("progress announced for an ineligible run: %v", payload)
	case <-time.After(200 * time.Millisecond):
	}
}

// A realtime reply to a newer, unrelated utterance must not mute a Harness
// update: the announcer waits for a free moment itself. Only the click does.
func TestHarnessUpdatesIgnoreRealtimeSupersedeButHonorClick(t *testing.T) {
	const run = "device-chat-270-1790318924951"
	later := int64(1790319247903) // someone chatted after the task was sent

	got := captureHALUpdates(t)
	h := &AgentHandler{}
	h.MarkHarnessResponseRun(run, false, false)
	h.autoSpeechWatermarkMs.Store(later)
	if !h.isSpeechCancelled(run) {
		t.Fatal("invalid setup: ordinary replies must still be superseded")
	}
	if err := h.SpeakHarnessGroupedResult("Story done.", "completed", []string{run}); err != nil {
		t.Fatalf("superseded grouped result: %v", err)
	}
	nextHALUpdate(t, got)
	h.MarkHarnessResponseRun("device-chat-271-1790318925000", false, false)
	if !h.DeliverHarnessQuestion("device-chat-271-1790318925000", "q1", "Which ending?") {
		t.Fatal("question not delivered")
	}
	if payload := nextHALUpdate(t, got); payload["kind"] != "question" {
		t.Fatalf("superseded question: %v", payload)
	}

	clicked := &AgentHandler{}
	clicked.MarkHarnessResponseRun(run, false, false)
	clicked.speechWatermarkMs.Store(later)
	err := clicked.SpeakHarnessGroupedResult("Story done.", "completed", []string{run})
	if !errors.Is(err, ErrHarnessResultSpeechSuppressed) {
		t.Fatalf("click must still silence the result, got %v", err)
	}
}
