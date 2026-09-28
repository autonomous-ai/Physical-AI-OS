package http

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	nethttp "net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// This opt-in integration test runs the production handler, HAL HTTP route and
// announcer queue, sanitized fallback and TTS worker. Cloud/realtime rendering
// is disabled; synthesis and the audio device are deterministic fixtures;
// nonzero PCM proves worker playback, not that a physical speaker was audible.
// It does not exercise OpenHarness transport or the durable result ledger.
func TestHarnessGroupedResultHALPlaybackIntegration(t *testing.T) {
	python := os.Getenv("HARNESS_HAL_TEST_PYTHON")
	if python == "" {
		t.Skip("set HARNESS_HAL_TEST_PYTHON to a Python environment with HAL test dependencies")
	}
	for _, scenario := range []string{"cancel-A-then-B", "cancel-A-then-B-reverse-members", "cancel-after-B", "muted", "singleton", "music-defers-playback"} {
		t.Run(scenario, func(t *testing.T) {
			fixture := startHarnessPlaybackFixture(t, python)
			h := &AgentHandler{}
			old := fmt.Sprintf("device-chat-1-%d", time.Now().Add(-time.Second).UnixMilli())
			h.MarkHarnessResponseRun(old, false, false)
			h.CancelSpeech()
			// A real cancel watermark is taken first. The new device timestamp must be
			// strictly greater even on a fast machine whose clock has not ticked yet.
			latest := fmt.Sprintf("device-chat-2-%d", h.speechWatermarkMs.Load()+1)
			h.MarkHarnessResponseRun(latest, false, false)
			ids := []string{old, latest}
			if scenario == "cancel-A-then-B-reverse-members" {
				ids = []string{latest, old}
			}
			if scenario == "singleton" {
				ids = []string{latest}
			}
			if scenario == "cancel-after-B" {
				// Use an actual later cancellation, rather than writing the watermark.
				deadline := time.NewTimer(2 * time.Millisecond)
				<-deadline.C
				h.CancelSpeech()
				if !h.isSpeechCancelled(latest) {
					t.Fatal("test did not cancel the newest input")
				}
			}
			if scenario == "muted" {
				fixture.mute(t)
			}
			fullText := "**All clouds around the airplane are now black.** The model preview has been updated. Full details are in out/model.glb."
			spokenText := "All clouds around the airplane are now black. The model preview has been updated."
			if scenario == "music-defers-playback" {
				fixture.music(t, true)
			}
			if !h.DeliverHarnessGroupedResult("playback-result", "completed", fullText, ids) {
				t.Fatal("result was not delivered")
			}
			err := h.SpeakHarnessGroupedResult(fullText, "completed", ids)
			suppressed := scenario == "cancel-after-B" || scenario == "muted"
			if suppressed {
				if !errors.Is(err, ErrHarnessResultSpeechSuppressed) {
					t.Fatalf("expected suppression, got %v", err)
				}
			} else if err != nil {
				t.Fatal(err)
			}
			// Match the production caller's admission gate: replay does not call the
			// speech sink again after the shared result's member routes are completed.
			if h.DeliverHarnessGroupedResult("playback-result", "completed", fullText, ids) {
				t.Fatal("replay was admitted for another speech submission")
			}
			if scenario == "music-defers-playback" {
				// The real announcer must retain the update until the gate opens.
				time.Sleep(600 * time.Millisecond)
				state := fixture.state(t)
				if state.SpeechFrames != 0 || state.CueFrames != 0 || len(state.Completions) != 0 {
					t.Fatalf("announcer spoke over music: %+v", state)
				}
				fixture.music(t, false)
			}
			state := fixture.waitState(t, !suppressed)
			expectedRequests := 1
			if scenario == "cancel-after-B" {
				expectedRequests = 0
			}
			if len(state.Requests) != expectedRequests {
				t.Fatalf("speak submissions=%d want=%d: %+v", len(state.Requests), expectedRequests, state)
			}
			for _, request := range state.Requests {
				if request.Text != fullText || request.TurnID != latest || request.Kind != "result" || request.Outcome != "completed" {
					t.Fatalf("wrong speech content/ownership: %+v", request)
				}
			}
			if suppressed {
				if state.SpeechFrames != 0 || state.CueFrames != 0 {
					t.Fatalf("suppressed result played audio: %+v", state)
				}
			} else if state.SpeechFrames == 0 || state.CueFrames == 0 || len(state.Completions) != 1 {
				t.Fatalf("expected speech PCM and one cue: %+v", state)
			}
			if !suppressed {
				cueGroups, speechNonzero := 0, 0
				lastKind := ""
				for _, write := range state.Writes {
					if write.Owner != "run:"+latest {
						t.Fatalf("PCM owned by wrong input: %+v", write)
					}
					if write.Kind == "cue" {
						if lastKind != "cue" {
							cueGroups++
						}
					} else {
						speechNonzero += write.Nonzero
					}
					lastKind = write.Kind
				}
				if cueGroups != 1 || speechNonzero == 0 {
					t.Fatalf("expected one cue and nonzero speech: %+v", state)
				}
				var completion struct {
					Owner   string `json:"owner"`
					History []any  `json:"history"`
				}
				if err := json.Unmarshal(state.Completions[0], &completion); err != nil {
					t.Fatal(err)
				}
				if completion.Owner != "run:"+latest || len(completion.History) != 3 || completion.History[0] != spokenText || completion.History[1] != true || completion.History[2] != false {
					t.Fatalf("incorrect completed history: %+v", completion)
				}

				if len(state.PlaybackOwners) != 1 || state.PlaybackOwners[0] != "run:"+latest {
					t.Fatalf("wrong playback attribution: %+v", state)
				}
			}
		})
	}
}

type harnessPlaybackRequest struct {
	Text    string `json:"text"`
	TurnID  string `json:"turn_id"`
	Kind    string `json:"kind"`
	Outcome string `json:"outcome"`
}
type harnessPlaybackState struct {
	Requests       []harnessPlaybackRequest `json:"requests"`
	SpeechFrames   int                      `json:"speech_frames"`
	CueFrames      int                      `json:"cue_frames"`
	Completions    []json.RawMessage        `json:"completions"`
	PlaybackOwners []string                 `json:"playback_owners"`
	Writes         []struct {
		Kind    string `json:"kind"`
		Owner   string `json:"owner"`
		Nonzero int    `json:"nonzero"`
	} `json:"writes"`
}
type harnessPlaybackFixture struct {
	base   string
	client *nethttp.Client
}
type harnessPlaybackTransport struct {
	target string
	base   nethttp.RoundTripper
}

func (r harnessPlaybackTransport) RoundTrip(req *nethttp.Request) (*nethttp.Response, error) {
	if req.URL.Host != "127.0.0.1:5001" {
		return r.base.RoundTrip(req)
	}
	clone := req.Clone(req.Context())
	copiedURL := *req.URL
	copiedURL.Host = r.target
	clone.URL = &copiedURL
	clone.Host = r.target
	return r.base.RoundTrip(clone)
}
func startHarnessPlaybackFixture(t *testing.T, python string) harnessPlaybackFixture {
	t.Helper()
	_, source, _, _ := runtime.Caller(0)
	script := filepath.Clean(filepath.Join(filepath.Dir(source), "../../../testdata/harness_hal_playback.py"))
	fixtureTimeout := 30 * time.Second
	if os.Getenv("HARNESS_TEST_MAC_SAY") == "1" {
		fixtureTimeout = 90 * time.Second
	}
	ctx, cancel := context.WithTimeout(context.Background(), fixtureTimeout)
	cmd := exec.CommandContext(ctx, python, script)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	// Avoid pipe saturation while preserving fixture failures in test output.
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		cancel()
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	t.Cleanup(func() { cancel(); <-done })
	ready := make(chan string, 1)
	go func() {
		scanner := bufio.NewScanner(stdout)
		for scanner.Scan() {
			line := scanner.Text()
			if strings.HasPrefix(line, "{") {
				ready <- line
				return
			}
		}
		ready <- ""
	}()
	var address struct {
		Port int `json:"port"`
	}
	select {
	case line := <-ready:
		if err := json.Unmarshal([]byte(line), &address); err != nil || address.Port == 0 {
			t.Fatalf("fixture did not report readiness: %q (%v)", line, err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("HAL fixture readiness timed out")
	}
	target := fmt.Sprintf("127.0.0.1:%d", address.Port)
	original := nethttp.DefaultTransport
	nethttp.DefaultTransport = harnessPlaybackTransport{target: target, base: original}
	t.Cleanup(func() { nethttp.DefaultTransport = original })
	return harnessPlaybackFixture{base: "http://" + target, client: &nethttp.Client{Transport: original, Timeout: 2 * time.Second}}
}
func (f harnessPlaybackFixture) music(t *testing.T, streaming bool) {
	t.Helper()
	response, err := f.client.Post(f.base+"/test/music", "application/json", strings.NewReader(fmt.Sprintf(`{"streaming":%t}`, streaming)))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		t.Fatalf("music: HTTP %d", response.StatusCode)
	}
}

func (f harnessPlaybackFixture) mute(t *testing.T) {
	t.Helper()
	response, err := f.client.Post(f.base+"/test/mute", "application/json", strings.NewReader(`{"muted":true}`))
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		body, _ := io.ReadAll(response.Body)
		t.Fatalf("mute: HTTP %d %s", response.StatusCode, body)
	}
}
func (f harnessPlaybackFixture) state(t *testing.T) harnessPlaybackState {
	t.Helper()
	response, err := f.client.Get(f.base + "/test/state")
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	var state harnessPlaybackState
	if response.StatusCode != 200 {
		t.Fatalf("state: HTTP %d", response.StatusCode)
	}
	if err := json.NewDecoder(response.Body).Decode(&state); err != nil {
		t.Fatal(err)
	}
	return state
}
func (f harnessPlaybackFixture) waitState(t *testing.T, playback bool) harnessPlaybackState {
	t.Helper()
	if !playback {
		return f.state(t)
	}
	playbackTimeout := 10 * time.Second
	if os.Getenv("HARNESS_TEST_MAC_SAY") == "1" {
		// Local speech synthesis can need time to initialize the macOS voice.
		playbackTimeout = 70 * time.Second
	}
	deadline := time.NewTimer(playbackTimeout)
	defer deadline.Stop()
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	for {
		state := f.state(t)
		if len(state.Completions) > 0 {
			return state
		}
		select {
		case <-deadline.C:
			t.Fatalf("HAL playback did not complete: %+v", state)
		case <-ticker.C:
		}
	}
}
