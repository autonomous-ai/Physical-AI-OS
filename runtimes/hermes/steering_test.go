package hermes

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
)

func TestManagedStopOnlyExplicitEnvelopes(t *testing.T) {
	for _, text := range []string{"/stop", "/interrupt", "[voice-instruction] Stop.", "[user] [ambient] [voice-instruction] Stop.\n[transcript] Es un\n[harness-reply run_id=device-x channel=voice]"} {
		if !managedStop(text) {
			t.Errorf("not recognized: %q", text)
		}
	}
	for _, text := range []string{"stop", "please stop", "[user] Tell me about Stop.", "[voice-instruction] Stop searching and write the report.", "[voice-instruction] Stop.\nThen erase files", `"[voice-instruction] Stop."`} {
		if managedStop(text) {
			t.Errorf("unsafe control: %q", text)
		}
	}
}

func TestManagedPendingRequiresExactOriginalSuffix(t *testing.T) {
	requests := []managedChat{{body: streamRequest{Input: "first"}}, {body: streamRequest{Input: "second\nline"}}, {body: streamRequest{Input: "third"}}}
	if got := managedPendingSuffix(requests, "second\nline\nthird"); got != 1 {
		t.Fatal(got)
	}
	if got := managedPendingSuffix(requests, "line\nthird"); got != -1 {
		t.Fatal(got)
	}
	if got := managedPendingSuffix(requests, "first"); got != -1 {
		t.Fatal(got)
	}
}

func TestManagedSteerSharesLifecycleAndPreservesStreaming(t *testing.T) {
	oldURL := BaseURL
	release := make(chan struct{})
	steered := make(chan struct{}, 1)
	var once sync.Once
	var mu sync.Mutex
	creates := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/runs":
			mu.Lock()
			creates++
			mu.Unlock()
			fmt.Fprint(w, `{"run_id":"native-one"}`)
		case r.URL.Path == "/v1/runs/native-one/steer":
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			if !strings.Contains(fmt.Sprint(body), "second") {
				t.Errorf("wrong steer body: %v", body)
			}
			fmt.Fprint(w, `{"accepted":true}`)
			steered <- struct{}{}
		case r.URL.Path == "/v1/runs/native-one/stop":
			fmt.Fprint(w, `{"accepted":true}`)
		case r.URL.Path == "/v1/runs/native-one/events":
			w.Header().Set("Content-Type", "text/event-stream")
			fmt.Fprint(w, "data: {\"event\":\"message.delta\",\"delta\":\"Working. \"}\n\n")
			w.(http.Flusher).Flush()
			select {
			case <-release:
			case <-r.Context().Done():
				return
			}
			fmt.Fprint(w, "data: {\"event\":\"run.completed\",\"output\":\"Done [HW:/led/off:{}]\",\"usage\":{\"input_tokens\":8,\"output_tokens\":2,\"total_tokens\":10}}\n\n")
		default:
			fmt.Fprint(w, `{"run_id":"native-one","status":"running","session_id":"session-one"}`)
		}
	}))
	BaseURL = server.URL
	ctx, cancel := context.WithCancel(context.Background())
	defer func() { once.Do(func() { close(release) }); cancel(); server.Close(); BaseURL = oldURL }()
	events := make(chan domain.WSEvent, 50)
	s := &HermesService{httpClient: server.Client(), runtimeCtx: ctx, silentRuns: map[string]bool{}, handler: func(_ context.Context, e domain.WSEvent) error { events <- e; return nil }}
	s.inFlightStreams.Store(2)
	s.enqueueManagedRun("original", streamRequest{Input: "first", Conversation: "conversation"}, "user")
	waitEvent := func(match func(map[string]any) bool) map[string]any {
		t.Helper()
		timeout := time.NewTimer(3 * time.Second)
		defer timeout.Stop()
		for {
			select {
			case e := <-events:
				var p map[string]any
				_ = json.Unmarshal(e.Payload, &p)
				if match(p) {
					return p
				}
			case <-timeout.C:
				t.Fatal("missing event")
				return nil
			}
		}
	}
	waitEvent(func(p map[string]any) bool { return p["stream"] == "assistant" })
	s.enqueueManagedRun("followup", streamRequest{Input: "second", Conversation: "conversation"}, "user")
	select {
	case <-steered:
	case <-time.After(3 * time.Second):
		t.Fatal("no steer")
	}
	waitEvent(func(p map[string]any) bool {
		d, _ := p["data"].(map[string]any)
		return p["runId"] == "followup" && d["phase"] == "start"
	})
	if s.inFlightStreams.Load() != 2 {
		t.Fatal("steer ack finished task")
	}
	once.Do(func() { close(release) })
	ends := map[string]bool{}
	chats := map[string]string{}
	assistant := map[string]string{}
	usageCount := 0
	deadline := time.NewTimer(3 * time.Second)
	defer deadline.Stop()
	for len(ends) < 2 {
		select {
		case e := <-events:
			var p map[string]any
			_ = json.Unmarshal(e.Payload, &p)
			id, _ := p["runId"].(string)
			if e.Event == "chat" {
				chats[id], _ = p["message"].(string)
			}
			d, _ := p["data"].(map[string]any)
			if p["stream"] == "assistant" {
				delta, _ := d["delta"].(string)
				assistant[id] += delta
			}
			if d["phase"] == "end" {
				ends[id] = true
				if d["usage"] != nil {
					usageCount++
				}
			}
		case <-deadline.C:
			t.Fatal("shared terminal missing")
		}
	}
	mu.Lock()
	count := creates
	mu.Unlock()
	if count != 1 {
		t.Fatalf("created %d independent runs", count)
	}
	if !s.IsSilentRun("original") {
		t.Fatal("old voice owner not suppressed")
	}
	if strings.Contains(chats["original"], "[HW:") || !strings.Contains(chats["followup"], "[HW:") {
		t.Fatalf("hardware ownership: %v", chats)
	}
	if assistant["followup"] != "Done [HW:/led/off:{}]" {
		t.Fatalf("new voice owner has no complete TTS/HW accumulator: %v", assistant)
	}
	if usageCount != 1 {
		t.Fatalf("usage count %d", usageCount)
	}
}

func TestManagedControlWaitsForTerminal(t *testing.T) {
	for _, mode := range []string{"rejected", "pending", "stop", "image"} {
		t.Run(mode, func(t *testing.T) {
			oldURL := BaseURL
			release := make(chan struct{})
			admitted := make(chan struct{}, 1)
			controlled := make(chan string, 2)
			var releaseOnce sync.Once
			var mu sync.Mutex
			creates := []map[string]any{}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				path := r.URL.Path
				switch {
				case path == "/v1/runs" && r.Method == http.MethodPost:
					var body map[string]any
					_ = json.NewDecoder(r.Body).Decode(&body)
					mu.Lock()
					creates = append(creates, body)
					n := len(creates)
					mu.Unlock()
					fmt.Fprintf(w, `{"run_id":"run-%d"}`, n)
				case strings.HasSuffix(path, "/steer"):
					if mode == "rejected" {
						w.WriteHeader(http.StatusConflict)
						fmt.Fprint(w, `{"detail":"not accepting"}`)
					} else {
						fmt.Fprint(w, `{"accepted":true}`)
					}
					controlled <- "steer"
				case strings.HasSuffix(path, "/stop"):
					fmt.Fprint(w, `{"accepted":true}`)
					select {
					case controlled <- "stop":
					default:
					}
				case strings.HasSuffix(path, "/events"):
					w.Header().Set("Content-Type", "text/event-stream")
					if strings.Contains(path, "run-1/") {
						fmt.Fprint(w, "data: {\"event\":\"message.delta\",\"delta\":\"Working\"}\n\n")
						w.(http.Flusher).Flush()
						admitted <- struct{}{}
						select {
						case <-release:
						case <-r.Context().Done():
							return
						}
						if mode == "stop" {
							fmt.Fprint(w, "data: {\"event\":\"run.cancelled\"}\n\n")
							return
						}
						pending := ""
						if mode == "pending" {
							pending = ",\"pending_steer\":\"second\""
						}
						fmt.Fprintf(w, "data: {\"event\":\"run.completed\",\"output\":\"First\"%s}\n\n", pending)
					} else {
						fmt.Fprint(w, "data: {\"event\":\"run.completed\",\"output\":\"Second\"}\n\n")
					}
				default:
					fmt.Fprint(w, `{"status":"running","session_id":"shared-session"}`)
				}
			}))
			BaseURL = server.URL
			ctx, cancel := context.WithCancel(context.Background())
			defer func() { releaseOnce.Do(func() { close(release) }); cancel(); server.Close(); BaseURL = oldURL }()
			events := make(chan domain.WSEvent, 100)
			s := &HermesService{httpClient: server.Client(), runtimeCtx: ctx, silentRuns: map[string]bool{}, handler: func(_ context.Context, e domain.WSEvent) error { events <- e; return nil }}
			s.inFlightStreams.Store(2)
			s.enqueueManagedRun("original", streamRequest{Input: "first", Conversation: "conv"}, "user")
			select {
			case <-admitted:
			case <-time.After(3 * time.Second):
				t.Fatal("not admitted")
			}
			var input any = "second"
			if mode == "stop" {
				input = "[user] [ambient] [voice-instruction] Stop.\n[transcript] Es un\n[harness-reply run_id=x channel=voice]"
			}
			if mode == "image" {
				input = []inputMessage{{Role: "user", Content: []inputContent{{Type: "input_text", Text: "second"}, {Type: "input_image", ImageURL: "data:image/jpeg;base64,YQ=="}}}}
			}
			s.enqueueManagedRun("followup", streamRequest{Input: input, Conversation: "conv"}, "user")
			if mode != "image" {
				select {
				case action := <-controlled:
					expected := "steer"
					if mode == "stop" {
						expected = "stop"
					}
					if action != expected {
						t.Fatal(action)
					}
				case <-time.After(3 * time.Second):
					t.Fatal("missing control")
				}
			}
			if s.inFlightStreams.Load() != 2 {
				t.Fatal("control ack changed busy/lifecycle")
			}
			mu.Lock()
			before := len(creates)
			mu.Unlock()
			if before != 1 {
				t.Fatal("created before old terminal")
			}
			releaseOnce.Do(func() { close(release) })
			phases := map[string]string{}
			deadline := time.NewTimer(3 * time.Second)
			defer deadline.Stop()
			for len(phases) < 2 {
				select {
				case e := <-events:
					var p map[string]any
					_ = json.Unmarshal(e.Payload, &p)
					d, _ := p["data"].(map[string]any)
					phase, _ := d["phase"].(string)
					if phase == "end" || phase == "error" {
						id, _ := p["runId"].(string)
						if _, exists := phases[id]; exists {
							t.Fatalf("duplicate terminal %s", id)
						}
						phases[id] = phase
					}
				case <-deadline.C:
					t.Fatal("missing terminal", phases)
				}
			}
			mu.Lock()
			recorded := append([]map[string]any(nil), creates...)
			mu.Unlock()
			expectedCreates := 2
			if mode == "stop" {
				expectedCreates = 1
			}
			if len(recorded) != expectedCreates {
				t.Fatalf("creates=%d", len(recorded))
			}
			if phases["followup"] != "end" {
				t.Fatalf("followup outcome %v", phases)
			}
			if mode == "stop" && phases["original"] != "error" {
				t.Fatal("cancelled work falsely completed")
			}
			if expectedCreates == 2 {
				if recorded[1]["session_id"] != "shared-session" {
					t.Fatalf("context lost: %v", recorded[1])
				}
				if mode != "image" && recorded[1]["input"] != "second" {
					t.Fatalf("wrong original replay: %v", recorded[1])
				}
				if mode == "image" {
					raw, _ := json.Marshal(recorded[1]["input"])
					if !strings.Contains(string(raw), "image_url") {
						t.Fatal(string(raw))
					}
				}
			}
			if mode == "stop" {
				for index, input := range []string{"New request after stop.", "Another request."} {
					id := fmt.Sprintf("after-stop-%d", index)
					s.inFlightStreams.Add(1)
					s.enqueueManagedRun(id, streamRequest{Input: input, Conversation: "conv"}, "user")
					finished := false
					for !finished {
						select {
						case event := <-events:
							var payload map[string]any
							_ = json.Unmarshal(event.Payload, &payload)
							data, _ := payload["data"].(map[string]any)
							finished = payload["runId"] == id && data["phase"] == "end"
						case <-deadline.C:
							t.Fatal("post-stop request did not finish")
						}
					}
					mu.Lock()
					body := creates[index+1]
					mu.Unlock()
					expected := input
					if index == 0 {
						expected = managedStopNotice + input
					}
					if body["input"] != expected {
						t.Fatalf("post-stop wire input: %v", body["input"])
					}
				}
			}

		})
	}
}

func TestManagedRequestsQueuedBeforeCreationSteerIntoOneRun(t *testing.T) {
	oldURL := BaseURL
	admitted := make(chan struct{}, 1)
	finish := make(chan struct{})
	var once sync.Once
	var creates int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/v1/runs" && r.Method == http.MethodPost:
			creates++
			fmt.Fprint(w, `{"run_id":"one"}`)
		case strings.HasSuffix(r.URL.Path, "/steer"):
			fmt.Fprint(w, `{"accepted":true}`)
			admitted <- struct{}{}
		case strings.HasSuffix(r.URL.Path, "/stop"):
			fmt.Fprint(w, `{"accepted":true}`)
		case strings.HasSuffix(r.URL.Path, "/events"):
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(200)
			w.(http.Flusher).Flush()
			select {
			case <-finish:
			case <-r.Context().Done():
				return
			}
			fmt.Fprint(w, "data: {\"event\":\"run.completed\",\"output\":\"Done\"}\n\n")
		default:
			fmt.Fprint(w, `{"status":"running","session_id":"session"}`)
		}
	}))
	BaseURL = server.URL
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	defer func() { once.Do(func() { close(finish) }); cancel(); <-done; server.Close(); BaseURL = oldURL }()
	s := &HermesService{httpClient: server.Client(), runtimeCtx: ctx, silentRuns: map[string]bool{}, steeringWake: make(chan struct{}, 1)}
	s.inFlightStreams.Store(2)
	s.steeringQueue = []managedChat{{runID: "first", body: streamRequest{Input: "first", Conversation: "same"}, source: "user"}, {runID: "second", body: streamRequest{Input: "second", Conversation: "same"}, source: "user"}}
	s.steeringWake <- struct{}{}
	go func() { s.managedLoop(); close(done) }()
	select {
	case <-admitted:
	case <-time.After(3 * time.Second):
		t.Fatal("second queued request was not steered")
	}
	if creates != 1 {
		t.Fatalf("%d creates", creates)
	}
	once.Do(func() { close(finish) })
	deadline := time.Now().Add(3 * time.Second)
	for s.inFlightStreams.Load() != 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if s.inFlightStreams.Load() != 0 {
		t.Fatal("terminal did not release early batch")
	}
}

func TestManagedCapabilityStaysNativeAfterUpgrade(t *testing.T) {
	oldURL := BaseURL
	probes := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/capabilities" {
			probes++
			http.Error(w, "unavailable", 503)
			return
		}
		fmt.Fprint(w, `{"status":"ok"}`)
	}))
	defer func() { server.Close(); BaseURL = oldURL }()
	BaseURL = server.URL
	s := &HermesService{httpClient: server.Client()}
	s.ready.Store(true)
	s.nativeRunSteering.Store(true)
	s.agentStartedAt.Store(1)
	s.probeHealth(context.Background())
	if !s.SupportsActiveTurnSteering() {
		t.Fatal("downgraded native session to legacy history")
	}
	if probes != 0 {
		t.Fatal("re-probed sticky capability")
	}
	s.nativeRunSteering.Store(false)
	s.inFlightStreams.Store(1)
	s.probeHealth(context.Background())
	if probes != 0 {
		t.Fatal("attempted transport change during legacy stream")
	}
}

func TestManagedWebSlashStopWithReplyRoute(t *testing.T) {
	payload := "/stop\n[harness-reply run_id=device-chat-5-1789449885863 channel=web]"
	if !managedUserInput(payload, "user_slash") || !managedStop(payload) {
		t.Fatal("routed web slash stop would be sent as steering text")
	}
	if !managedStop("/interrupt\n[harness-reply run_id=device-chat-5-1789449885863 channel=web]") {
		t.Fatal("routed interrupt not recognized")
	}
	for _, text := range []string{
		payload + "\nThen write a report",
		"/stop searching\n[harness-reply run_id=x channel=web]",
		"/stop\n[harness-reply run_id=x channel=web] then write a report",
		"/stop\n[harness-reply run_id=x channel=web",
		"/stop\n[transcript] continue the task",
	} {
		if managedStop(text) {
			t.Errorf("meaningful suffix treated as stop: %q", text)
		}
	}
}

func TestManagedFinalEmitsOnlyMissingAssistantSuffix(t *testing.T) {
	for _, streamed := range []string{"", "Hello ", "Hello world."} {
		t.Run(fmt.Sprintf("streamed=%q", streamed), func(t *testing.T) {
			var delta string
			s := &HermesService{handler: func(_ context.Context, event domain.WSEvent) error {
				var p map[string]any
				_ = json.Unmarshal(event.Payload, &p)
				if p["stream"] == "assistant" {
					data, _ := p["data"].(map[string]any)
					text, _ := data["delta"].(string)
					delta += text
				}
				return nil
			}}
			s.inFlightStreams.Store(1)
			active := &managedTurn{owner: "voice", requests: []managedChat{{runID: "voice"}}, streamed: map[string]string{"voice": streamed}}
			s.finishManaged(context.Background(), active, streamResult{Terminal: true, FinalText: "Hello world."}, nil)
			if streamed+delta != "Hello world." {
				t.Fatalf("duplicated or missing speech: %q + %q", streamed, delta)
			}
		})
	}
}

func TestManagedStopContextFollowsConfirmedUserStopOnly(t *testing.T) {
	request := managedChat{runID: "new", source: "user", body: streamRequest{Conversation: "same", Input: "Read the camera."}}
	var state managedStopContext
	state.observe(&managedTurn{stopping: true, conversation: "same"}, streamResult{Terminal: false})
	if _, used := state.prepare(request); used {
		t.Fatal("stop acknowledgement is not a cancelled terminal")
	}
	state.observe(&managedTurn{conversation: "same"}, streamResult{Terminal: true, Errored: true})
	if _, used := state.prepare(request); used {
		t.Fatal("transport cleanup cancellation is not a user stop")
	}
	state.observe(&managedTurn{stopping: true, conversation: "same"}, streamResult{Terminal: true, Errored: true})
	for _, passive := range []managedChat{{source: "system", body: streamRequest{Conversation: "same", Input: "Keepalive"}}, {source: "user", body: streamRequest{Conversation: "same", Input: "[sensing:presence.enter] user"}}} {
		if _, used := state.prepare(passive); used {
			t.Fatal("passive input consumed cancellation context")
		}
	}
	wire, used := state.prepare(request)
	if !used || wire.Input != managedStopNotice+"Read the camera." {
		t.Fatalf("missing cancellation/new input boundary: %v", wire.Input)
	}
	if request.body.Input != "Read the camera." {
		t.Fatal("mutated original correlation body")
	}
	if _, used = state.prepare(request); !used {
		t.Fatal("notice consumed before admission")
	}
	request.body.Conversation = "new-session"
	if _, used = state.prepare(request); used || state.conversation != "" {
		t.Fatal("cancellation leaked across conversations")
	}
}

func TestManagedStopContextPreservesImageAndOriginalBody(t *testing.T) {
	original := []inputMessage{{Role: "user", Content: []inputContent{{Type: "input_text", Text: "Describe this image."}, {Type: "input_image", ImageURL: "data:image/jpeg;base64,YQ=="}}}}
	before, _ := json.Marshal(original)
	state := managedStopContext{conversation: "same"}
	wire, used := state.prepare(managedChat{source: "user", body: streamRequest{Conversation: "same", Input: original}})
	if !used {
		t.Fatal("missing multimodal stop context")
	}
	canonical, err := managedRunInput(wire.Input)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(canonical)
	if !strings.Contains(string(raw), "explicitly stopped") || !strings.Contains(string(raw), "Describe this image.") || !strings.Contains(string(raw), "data:image/jpeg;base64,YQ==") {
		t.Fatalf("lost request content: %s", raw)
	}
	after, _ := json.Marshal(original)
	if string(before) != string(after) {
		t.Fatal("mutated original image request")
	}
}

// hermes-gateway restarting exactly when a run is created (presync config change on lamp-0c4e 2026-09-16) yields a dial error.
func TestManagedDialFailureDoesNotPoisonConversation(t *testing.T) {
	oldURL := BaseURL
	dead := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	deadURL := dead.URL
	dead.Close()
	var mu sync.Mutex
	creates := 0
	live := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/runs":
			mu.Lock()
			creates++
			mu.Unlock()
			fmt.Fprint(w, `{"run_id":"native-one"}`)
		case strings.HasSuffix(r.URL.Path, "/events"):
			w.Header().Set("Content-Type", "text/event-stream")
			fmt.Fprint(w, "data: {\"event\":\"run.completed\",\"output\":\"Done\",\"usage\":{\"input_tokens\":1,\"output_tokens\":1,\"total_tokens\":2}}\n\n")
		default:
			fmt.Fprint(w, `{"run_id":"native-one","status":"running","session_id":"session-one"}`)
		}
	}))
	BaseURL = deadURL
	ctx, cancel := context.WithCancel(context.Background())
	defer func() { cancel(); live.Close(); BaseURL = oldURL }()
	events := make(chan domain.WSEvent, 50)
	s := &HermesService{httpClient: live.Client(), runtimeCtx: ctx, silentRuns: map[string]bool{}, handler: func(_ context.Context, e domain.WSEvent) error { events <- e; return nil }}
	waitPhase := func(runID, phase string) map[string]any {
		t.Helper()
		timeout := time.NewTimer(5 * time.Second)
		defer timeout.Stop()
		for {
			select {
			case e := <-events:
				var p map[string]any
				_ = json.Unmarshal(e.Payload, &p)
				d, _ := p["data"].(map[string]any)
				if p["runId"] == runID && d["phase"] == phase {
					return d
				}
			case <-timeout.C:
				t.Fatalf("missing %s/%s", runID, phase)
				return nil
			}
		}
	}
	conversation := s.conversationName()
	s.inFlightStreams.Store(1)
	s.enqueueManagedRun("during-restart", streamRequest{Input: "first", Conversation: conversation}, "user")
	if d := waitPhase("during-restart", "error"); !strings.Contains(fmt.Sprint(d["error"]), "create native Hermes run") {
		t.Fatalf("unexpected error: %v", d["error"])
	}
	if s.conversationName() != conversation {
		t.Fatal("conversation rotated on a dial failure — the prompt was never delivered")
	}
	BaseURL = live.URL
	s.inFlightStreams.Add(1)
	s.enqueueManagedRun("after-restart", streamRequest{Input: "next", Conversation: conversation}, "user")
	waitPhase("after-restart", "end")
	mu.Lock()
	defer mu.Unlock()
	if creates != 1 {
		t.Fatalf("created %d runs after the gateway came back", creates)
	}
}
