// Run this optional local mDNS integration check against an installed Harness CLI checkout.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"syscall"
	"time"

	"go.autonomous.ai/os/system/harness"
)

func must(ok bool, msg string) {
	if !ok {
		panic(msg)
	}
}
func wait(label string, f func() bool) {
	deadline := time.Now().Add(25 * time.Second)
	for time.Now().Before(deadline) {
		if f() {
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
	panic("timeout " + label)
}
func main() {
	if len(os.Args) != 2 {
		panic("usage: go run ./system/harness/testdata/direct_interop.go /absolute/path/to/harness/cli")
	}
	cliRoot, e := filepath.Abs(os.Args[1])
	if e != nil {
		panic(e)
	}
	_, source, _, _ := runtime.Caller(0)

	// Invoke Node directly rather than through a tsx shebang.
	node, e := exec.LookPath("node")
	must(e == nil, "node executable missing")
	cmd := exec.Command(node, "--import", filepath.Join(cliRoot, "node_modules/tsx/dist/loader.mjs"), filepath.Join(filepath.Dir(source), "direct-interop.mts"))
	cmd.Env = append(os.Environ(), "HARNESS_CLI_ROOT="+cliRoot)
	out, _ := cmd.StdoutPipe()
	cmd.Stderr = os.Stderr
	must(cmd.Start() == nil, "fixture start")
	defer func() {
		cmd.Process.Signal(syscall.SIGTERM)
		done := make(chan struct{})
		go func() { cmd.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			cmd.Process.Kill()
			<-done
		}
	}()
	readyCh := make(chan map[string]any, 1)
	go func() {
		sc := bufio.NewScanner(out)
		sent := false
		for sc.Scan() {
			var frame map[string]any
			if !sent && json.Unmarshal(sc.Bytes(), &frame) == nil && frame["port"] != nil {
				readyCh <- frame
				sent = true
			}
		}
		if !sent {
			readyCh <- nil
		}
	}()
	var ready map[string]any
	select {
	case ready = <-readyCh:
		must(ready["port"] != nil, "fixture exited before announcing its port")
	case <-time.After(20 * time.Second):
		panic("fixture startup timed out after 20 seconds")
	}
	base := fmt.Sprintf("http://127.0.0.1:%.0f", ready["port"])
	httpClient := &http.Client{Timeout: 35 * time.Second}
	api := func(path string, body any) map[string]any {
		b, _ := json.Marshal(body)
		r, e := httpClient.Post(base+path, "application/json", bytes.NewReader(b))
		if e != nil {
			panic(e)
		}
		defer r.Body.Close()
		raw, _ := io.ReadAll(r.Body)
		var f map[string]any
		json.Unmarshal(raw, &f)
		must(r.StatusCode == 200, "fixture error: "+string(raw))
		return f
	}
	dir, _ := os.MkdirTemp("", "os-direct-test-")
	defer os.RemoveAll(dir)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s, e := harness.NewService(dir, harness.Callbacks{})
	if e != nil {
		panic(e)
	}
	s.Start(ctx)
	server := httptest.NewUnstartedServer(s)
	listener, e := net.Listen("tcp", "0.0.0.0:0")
	if e != nil {
		panic(e)
	}
	server.Listener = listener
	server.Start()
	defer server.Close()
	port := listener.Addr().(*net.TCPAddr).Port
	ad := api("/advertise", map[string]any{"port": port})
	var deviceID string
	for i := 0; i < 3 && deviceID == ""; i++ {
		rows := api("/discover", nil)["devices"].([]any)
		for _, row := range rows {
			d := row.(map[string]any)
			if d["name"] == ad["name"] {
				deviceID = d["id"].(string)
				must(d["port"] == float64(port), "discovery ignored SRV port")
			}
		}
	}
	must(deviceID != "", "real mDNS discovery failed")
	info, e := s.StartPair(ctx)
	must(e == nil && len(info.Code) == 6, "OS generate code")
	wrong := "000000"
	if info.Code == wrong {
		wrong = "111111"
	}
	r := api("/pair", map[string]any{"id": deviceID, "code": wrong})
	must(r["error"] == "CODE_MISMATCH", "wrong code not reported: "+fmt.Sprint(r))
	wait("failed pairing cleanup", func() bool { return !s.PairStatus().Pairing })
	must(!s.Status().Paired, "wrong code wrote pin")
	info, e = s.StartPair(ctx)
	must(e == nil, "new window after mismatch")
	r = api("/pair", map[string]any{"id": deviceID, "code": info.Code})
	must(r["state"] == "paired", "pair failed: "+fmt.Sprint(r))
	fp := r["fingerprint"]
	wait("OS connected", func() bool { return s.Status().Connected })
	must(s.PairStatus().Code == "", "code persisted after completion")
	r, e = s.Request(ctx, harness.Frame{"type": "agents.list"})
	must(e == nil && r["machineId"] == "interop-machine", "encrypted list failed")
	// The optional recap headline must cross the encrypted path untouched.
	listed := map[string]map[string]any{}
	if agents, _ := r["agents"].([]any); agents != nil {
		for _, a := range agents {
			if row, ok := a.(map[string]any); ok {
				id, _ := row["agentId"].(string)
				listed[id] = row
			}
		}
	}
	must(listed["agent-one"] != nil && listed["agent-one"]["recap"] == "Fixed reconnect in client.ts", "agents.list recap headline missing: "+fmt.Sprint(r["agents"]))
	_, hasRecap := listed["agent-two"]["recap"]
	must(listed["agent-two"] != nil && !hasRecap, "agents.list recap present for an agent without a summary")
	r, e = s.Request(ctx, harness.Frame{"type": "recap", "machineId": "interop-machine", "agentId": "agent-one", "n": 1})
	must(e == nil, "encrypted recap failed")
	if turns, _ := r["turns"].([]any); len(turns) == 1 {
		turn, _ := turns[0].(map[string]any)
		must(turn["recap"] == listed["agent-one"]["recap"], "agents.list recap differs from recap turns[0].recap")
	} else {
		panic("recap n:1 did not return one turn: " + fmt.Sprint(r))
	}
	req := harness.Frame{"type": "turn.send", "machineId": "interop-machine", "agentId": "agent-one", "text": "interop", "idempotencyKey": "direct-test"}
	r, e = s.Request(ctx, req)
	must(e == nil && r["status"] == "accepted", "send failed")
	r, e = s.Request(ctx, req)
	must(e == nil && r["status"] == "duplicate", "dedupe failed")
	state := api("/state", nil)
	must(state["submits"] == float64(1), "duplicate dispatched")
	must(state["backend"] == false && state["commander"] == true, "direct requires backend or recap presence absent")
	// Exercise the production controller over the authenticated encrypted link.
	dispatched := 0
	expectedAgent := "agent-one"
	var nextQuestion string
	voice := harness.NewVoiceController(s, harness.VoiceCallbacks{
		OnDispatch: func(agentID, runID string) {
			must(agentID == expectedAgent && runID != "", "voice response route missing")
			dispatched++
		},
		OnResponse: func(agentID, runID, text string) { nextQuestion = text },
	})
	must(!voice.State().Enabled, "voice default is not off")
	offGeneration := voice.State().Generation
	api("/focus", map[string]any{"agentId": "agent-one"})
	must(voice.RefreshFocus(ctx) == nil, "voice focus refresh while off failed")
	mode := voice.State()
	must(!mode.Enabled && mode.AgentID == "agent-one" && mode.FocusAvailable && mode.Generation == offGeneration, "off focus mirror changed capture generation")
	mode, e = voice.SetMode(ctx, true)
	must(e == nil && mode.Enabled, "voice enable failed")
	e = voice.Submit(ctx, "voice interop", "voice-one", mode.Generation)
	must(e == nil, fmt.Sprintf("voice encrypted submit failed: %v", e))
	must(voice.Submit(ctx, "voice interop", "voice-one", mode.Generation) == nil, "voice duplicate failed")
	state = api("/state", nil)
	must(state["submits"] == float64(2) && dispatched == 1, "voice duplicate dispatched")
	api("/question", map[string]any{"requestId": "voice-question", "questions": []harness.VoiceQuestion{
		{Key: "branch", Q: "Which branch?", Options: []string{"main", "feature"}},
		{Key: "checks", Q: "Which checks?", Options: []string{"lint", "tests"}, Multi: true},
	}})
	question, e := voice.Question(ctx)
	must(e == nil && question["questionRequestId"] == "voice-question", "voice live question failed")
	must(voice.Submit(ctx, "feature", "voice-answer-one", mode.Generation) == nil, "voice first answer failed")
	must(nextQuestion == "Which checks?\nlint, tests", "voice sequential question prompt missing")
	state = api("/state", nil)
	must(len(state["answers"].([]any)) == 0, "partial questionnaire dispatched")
	must(voice.Submit(ctx, "lint, tests", "voice-answer-two", mode.Generation) == nil, "voice second answer failed")
	state = api("/state", nil)
	answers := state["answers"].([]any)
	must(len(answers) == 1 && dispatched == 2, "questionnaire did not dispatch exactly once")
	answer := answers[0].(map[string]any)
	values := answer["answers"].(map[string]any)
	must(answer["agentId"] == "agent-one" && answer["requestId"] == "voice-question" && values["branch"] == "feature" && values["checks"] == "lint, tests", "voice answer wire contract mismatch")
	question, e = voice.Question(ctx)
	_, noQuestion := question["question"]
	must(e == nil && noQuestion && question["question"] == nil, "answered question remained open")
	// Let the real per-connection request bucket replenish between test batches.
	time.Sleep(10 * time.Second)
	// An app focus move invalidates a captured utterance before any dispatch.
	oldMode := mode
	api("/focus", map[string]any{"agentId": "agent-two"})
	must(voice.Submit(ctx, "must not reach either agent", "voice-old-focus", oldMode.Generation) != nil, "old capture survived focus change")
	mode = voice.State()
	must(mode.AgentID == "agent-two" && mode.Generation != oldMode.Generation, "focus move did not update voice generation")
	state = api("/state", nil)
	must(state["submits"] == float64(2), "stale captured utterance dispatched")
	// The CLI also checks the revision if focus changes after OS checked it.
	r, e = s.Request(ctx, harness.Frame{"type": "turn.send", "machineId": "interop-machine", "agentId": "agent-one", "text": "must not dispatch", "idempotencyKey": "stale-focus", "focusRevision": oldMode.FocusRevision})
	must(e == nil, "guarded focus request transport failed")
	guardError, ok := r["error"].(map[string]any)
	must(ok && guardError["code"] == "FOCUS_CHANGED" && r["receipt"] == nil, "CLI did not reject stale focus before reservation")
	state = api("/state", nil)
	must(state["submits"] == float64(2), "CLI stale focus guard dispatched")
	expectedAgent = "agent-two"
	e = voice.Submit(ctx, "fresh focused task", "voice-new-focus", mode.Generation)
	must(e == nil, fmt.Sprintf("fresh focused capture failed: %v", e))
	state = api("/state", nil)
	targets := state["submittedAgents"].([]any)
	must(state["submits"] == float64(3) && targets[len(targets)-1] == "agent-two" && dispatched == 3, "fresh task did not reach app-focused agent")
	_, e = voice.SetMode(ctx, false)
	must(e == nil, "voice disable failed")
	must(voice.Submit(ctx, "must not send", "voice-off", mode.Generation) != nil, "disabled voice accepted input")
	state = api("/state", nil)
	must(state["submits"] == float64(3) && len(state["answers"].([]any)) == 1, "disabled voice sent mutation")
	time.Sleep(5 * time.Second)
	api("/focus", map[string]any{"agentId": nil})
	mode, e = voice.ToggleGesture(ctx, "interop-gesture")
	must(e == nil && mode.Enabled && mode.AgentID == "agent-one", fmt.Sprintf("gesture first-agent focus failed: %v", e))
	duplicateMode, e := voice.ToggleGesture(ctx, "interop-gesture")
	must(e == nil && duplicateMode.Generation == mode.Generation && duplicateMode.Enabled, "gesture retry toggled twice")
	mode, e = voice.ToggleGesture(ctx, "interop-gesture-off")
	must(e == nil && !mode.Enabled, "gesture disable failed")
	state = api("/state", nil)
	must(state["submits"] == float64(3), "focus selection submitted an agent task")
	api("/restart", nil)
	wait("CLI restart reconnect", func() bool { return s.Status().Connected })
	time.Sleep(3500 * time.Millisecond)
	r, e = s.Request(ctx, harness.Frame{"type": "agents.list"})
	must(e == nil, "request after reconnect")
	cancel()
	server.Close()
	ctx2, cancel2 := context.WithCancel(context.Background())
	defer cancel2()
	s2, e := harness.NewService(dir, harness.Callbacks{})
	must(e == nil, "reload OS trust")
	s2.Start(ctx2)
	next := httptest.NewUnstartedServer(s2)
	listener2, e := net.Listen("tcp", ":"+strconv.Itoa(port))
	must(e == nil, "restart listener")
	next.Listener = listener2
	next.Start()
	defer next.Close()
	api("/restart", nil)
	wait("OS restart reconnect", func() bool { return s2.Status().Connected })
	r, e = s2.Request(ctx2, harness.Frame{"type": "agents.list"})
	must(e == nil, "list after OS restart")
	api("/revoke", map[string]any{"fingerprint": fp})
	wait("revoke disconnect", func() bool { return !s2.Status().Connected })
	// App-side revoke sends a sealed pair.revoke; the OS drops its own pin.
	wait("revoke unpairs OS", func() bool { return s2.Status().State == "unpaired" })
	_, e = s2.Request(ctx2, harness.Frame{"type": "agents.list"})
	must(e != nil, "revoked access remained")
	must(s2.Unpair() == nil && !s2.Status().Paired, "OS unpair")
	fmt.Println("PASS real mDNS + actual BackendSocket/E2eeManager + direct client + Go Service: wrong-code failure/retry, pair, encrypted list with recap headline/recap/send/dedupe, app focus mirror/old capture rejection/CLI revision guard/focused submit/dedupe/sequential question answers/off, CLI and OS restart reconnect, revoke/unpair. Backend never connected.")
}
