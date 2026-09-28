package server

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.autonomous.ai/os/system/harness"
	agenthttp "go.autonomous.ai/os/system/server/agent/delivery/http"
)

func resultServer(t *testing.T) (*Server, harness.ResultContext) {
	t.Helper()
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	s := &Server{agentHandler: &agenthttp.AgentHandler{}}
	if err := s.initializeHarnessResults(filepath.Join(root, "results.json")); err != nil {
		t.Fatal(err)
	}
	return s, harness.ResultContext{Owner: "owner", ServerInstanceID: "instance", MachineID: "machine"}
}
func reserveServerInput(t *testing.T, s *Server, p harness.ResultContext, key string) {
	t.Helper()
	frame := harness.Frame{"type": "turn.send", "agentId": "agent", "idempotencyKey": key, "text": "task " + key}
	s.registerHarnessDispatch("agent", "run-"+key, true, true, frame)
	if err := s.reserveHarnessResult(frame, p); err != nil {
		t.Fatal(err)
	}
}
func serverReceipt(key string) harness.Frame {
	return harness.Frame{"receipt": map[string]any{"serverInstanceId": "instance", "agentId": "agent", "idempotencyKey": key, "deliveryId": "delivery-" + key}}
}
func serverGroup(keys ...string) harness.Frame {
	inputs := []any{}
	for _, key := range keys {
		inputs = append(inputs, map[string]any{"idempotencyKey": key, "deliveryId": "delivery-" + key})
	}
	scope := "group"
	if len(keys) == 1 {
		scope = "input"
	}
	return harness.Frame{"type": "event", "kind": "turn.summary", "agentId": "agent", "payload": map[string]any{
		"serverInstanceId": "instance", "resultId": "result", "fullText": "House with trees.", "outcome": "completed",
		"correlation": map[string]any{"scope": scope, "inputs": inputs},
	}}
}
func TestHarnessResultInboxWaitsForAllReceiptsThenClosesOnlyMembers(t *testing.T) {
	s, p := resultServer(t)
	for _, key := range []string{"a", "b", "c"} {
		reserveServerInput(t, s, p, key)
	}
	if err := s.captureHarnessResult(serverGroup("a", "b"), p); err != nil {
		t.Fatal(err)
	}
	s.bindHarnessResultReceipt(serverReceipt("a"), p)
	s.processHarnessResults(p)
	if len(s.harnessResults.Results()) != 0 || !s.hasHarnessReply("agent", "run-a") {
		t.Fatal("partially applied unresolved group")
	}
	s.bindHarnessResultReceipt(serverReceipt("b"), p)
	s.processHarnessResults(p)
	if s.hasHarnessReply("agent", "run-a") || s.hasHarnessReply("agent", "run-b") || !s.hasHarnessReply("agent", "run-c") {
		t.Fatal("closed wrong members")
	}
	rows := s.harnessResults.Results()
	if len(rows) != 1 || rows[0].SpeechState != "suppressed" {
		t.Fatalf("web result speech: %+v", rows)
	}
	if err := s.captureHarnessResult(serverGroup("b", "a"), p); err != nil {
		t.Fatal(err)
	}
	s.processHarnessResults(p)
	if len(s.harnessResults.Results()) != 1 || !s.hasHarnessReply("agent", "run-c") {
		t.Fatal("replay changed membership")
	}
}
func TestHarnessResultAuthenticatedProvenanceAndExplicitMismatch(t *testing.T) {
	s, p := resultServer(t)
	reserveServerInput(t, s, p, "a")
	for _, field := range []string{"serverInstanceId", "machineId"} {
		f := serverGroup("a")
		f[field] = "wrong"
		if err := s.captureHarnessResult(f, p); err == nil {
			t.Fatalf("wrong transport %s accepted", field)
		}
	}
	unauthenticated := p
	unauthenticated.Owner = ""
	if err := s.captureHarnessResult(serverGroup("a"), unauthenticated); err == nil {
		t.Fatal("missing authenticated owner accepted")
	}
	if err := s.captureHarnessResult(serverGroup("a"), p); err != nil {
		t.Fatal(err)
	}
	receipt := serverReceipt("a")
	receipt["receipt"].(map[string]any)["agentId"] = "wrong-agent"
	s.bindHarnessResultReceipt(receipt, p)
	s.processHarnessResults(p)
	if len(s.harnessResults.Results()) != 0 {
		t.Fatal("wrong receipt agent completed result")
	}
	other := p
	other.Owner = "other-owner"
	s.bindHarnessResultReceipt(serverReceipt("a"), other)
	if err := s.captureHarnessResult(serverGroup("a"), other); err != nil {
		t.Fatal(err)
	}
	s.processHarnessResults(other)
	if len(s.harnessResults.Results()) != 0 || !s.hasHarnessReply("agent", "run-a") {
		t.Fatal("other owner completed result")
	}
	s.bindHarnessResultReceipt(serverReceipt("a"), p)
	s.processHarnessResults(p)
	frame := serverGroup("a")
	frame["payload"].(map[string]any)["fullText"] = "changed"
	if err := s.captureHarnessResult(frame, p); err == nil {
		t.Fatal("changed content accepted")
	}
}
func TestHarnessResultOriginalInstanceReconcilesAfterDaemonRestart(t *testing.T) {
	s, p := resultServer(t)
	reserveServerInput(t, s, p, "a")
	current := p
	current.ServerInstanceID = "new-daemon"
	frame := serverGroup("a")
	frame["serverInstanceId"] = current.ServerInstanceID
	frame["machineId"] = current.MachineID
	if err := s.captureHarnessResult(frame, current); err != nil {
		t.Fatal(err)
	}
	s.processHarnessResults(current)
	if len(s.harnessResults.Results()) != 0 {
		t.Fatal("result completed without original receipt")
	}
	// Receipt retains original instance, independently of the live transport.
	s.bindHarnessResultReceipt(serverReceipt("a"), current)
	s.processHarnessResults(current)
	rows := s.harnessResults.Results()
	if len(rows) != 1 || rows[0].Payload.ServerInstanceID != "instance" || s.hasHarnessReply("agent", "run-a") {
		t.Fatalf("original instance not reconciled: %+v", rows)
	}
}
func TestHarnessResultReservationFailureAndRetry(t *testing.T) {
	s, p := resultServer(t)
	frame := harness.Frame{"type": "turn.send", "agentId": "agent", "idempotencyKey": "a", "text": "task a"}
	if err := s.reserveHarnessResult(frame, p); err != nil || len(s.harnessResults.Inputs()) != 0 {
		t.Fatal("unrouted command unexpectedly reserved", err)
	}
	reserveServerInput(t, s, p, "a")
	deadline := s.harnessResults.Inputs()[0].ExpiresAt
	s.registerHarnessDispatch("agent", "run-a", true, true, frame)
	if err := s.reserveHarnessResult(frame, p); err != nil {
		t.Fatal(err)
	}
	if !s.harnessResults.Inputs()[0].ExpiresAt.Equal(deadline) {
		t.Fatal("retry extended expiration")
	}
	frame["text"] = "different intent"
	if err := s.reserveHarnessResult(frame, p); err == nil {
		t.Fatal("same key changed intent")
	}
	s.harnessResults = nil
	if err := s.reserveHarnessResult(frame, p); err == nil {
		t.Fatal("missing durable store allowed dispatch")
	}
}
func TestHarnessResultLifecycleAndReceiptCannotReplaceSummary(t *testing.T) {
	s, p := resultServer(t)
	reserveServerInput(t, s, p, "a")
	for _, kind := range []string{"turn.done", "receipt.updated"} {
		s.forwardHarnessEvent(harness.Frame{"kind": kind, "agentId": "agent", "idempotencyKey": "a", "payload": map[string]any{"state": "completed", "text": "legacy", "fullText": "legacy", "receipt": map[string]any{"state": "completed", "idempotencyKey": "a"}}})
		if !s.hasHarnessReply("agent", "run-a") || s.HarnessFollowupContext() != "" {
			t.Fatal("non-summary lifecycle consumed result")
		}
	}
}
func TestHarnessResultMalformedSummaryNeverFallsBack(t *testing.T) {
	for _, field := range []string{"resultId", "serverInstanceId", "correlation", "outcome"} {
		t.Run(field, func(t *testing.T) {
			s, p := resultServer(t)
			reserveServerInput(t, s, p, "a")
			f := harness.Frame{"type": "event", "kind": "turn.summary", "agentId": "agent", "idempotencyKey": "a", "payload": map[string]any{"fullText": "unsafe fallback", field: nil}}
			if err := s.captureHarnessResult(f, p); err == nil {
				t.Fatal("malformed metadata accepted")
			}
			s.forwardHarnessEvent(f)
			if !s.hasHarnessReply("agent", "run-a") || len(s.harnessResults.Results()) != 0 || s.HarnessFollowupContext() != "" {
				t.Fatal("malformed summary used legacy route")
			}
		})
	}
}
func TestHarnessResultRestartRetainsInboxWithoutRevivingSpeech(t *testing.T) {
	s, p := resultServer(t)
	reserveServerInput(t, s, p, "a")
	s.bindHarnessResultReceipt(serverReceipt("a"), p)
	if err := s.captureHarnessResult(serverGroup("a"), p); err != nil {
		t.Fatal(err)
	}
	// Persist/reopen the inbox with no current speaker owner.
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "restart.json")
	// Reserve/stage in another file, then initialize a fresh server against it.
	store, err := harness.OpenResultStore(path)
	if err != nil {
		t.Fatal(err)
	}
	in := s.harnessResults.Inputs()[0]
	in.DeliveryID = ""
	in.Channel = "voice"
	in.Destination = "voice:main:0"
	if err := store.Reserve(in); err != nil {
		t.Fatal(err)
	}
	if err := store.BindReceipt(p.Owner, p.ServerInstanceID, "agent", "a", "delivery-a"); err != nil {
		t.Fatal(err)
	}
	if err := store.Stage(p.Owner, serverGroup("a")); err != nil {
		t.Fatal(err)
	}
	restarted := &Server{}
	if err := restarted.initializeHarnessResults(path); err != nil {
		t.Fatal(err)
	}
	restarted.processHarnessResults(p)
	rows := restarted.harnessResults.Results()
	if len(rows) != 1 || rows[0].SpeechState != "suppressed" {
		t.Fatal("restart revived speech", rows)
	}
}
func TestHarnessResultDefaultInitializesStore(t *testing.T) {
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	s := &Server{agentHandler: &agenthttp.AgentHandler{}}
	path := filepath.Join(root, "storage", "results.json")
	if err := s.initializeHarnessResults(path); err != nil {
		t.Fatal(err)
	}
	if s.harnessResults == nil {
		t.Fatal("default did not initialize ledger")
	}
	p := harness.ResultContext{Owner: "owner", MachineID: "machine", ServerInstanceID: "instance"}
	reserveServerInput(t, s, p, "a")
	if _, err := os.Stat(path); err != nil {
		t.Fatal("reservation not durable", err)
	}
}
func TestHarnessInputProgressNeverClaimsTaskCompletion(t *testing.T) {
	for _, mode := range []string{"direct", "steering", "native_queue", "native_input", "daemon_queue"} {
		f := harness.Frame{"kind": "receipt.updated", "payload": map[string]any{"receipt": map[string]any{"state": "completed", "input": map[string]any{"mode": mode, "phase": "accepted"}}}}
		text := harnessInputProgress(f)
		if text == "" || strings.Contains(text, "completed") || strings.Contains(text, "finished") {
			t.Fatal("input acceptance conflated with completion", text)
		}
	}
}

// Ensure the shared result is emitted once; references are not full answers.
func TestHarnessResultSinkReferencesShareOneResult(t *testing.T) {
	s, p := resultServer(t)
	for _, key := range []string{"a", "b"} {
		reserveServerInput(t, s, p, key)
		s.bindHarnessResultReceipt(serverReceipt(key), p)
	}
	if err := s.captureHarnessResult(serverGroup("a", "b"), p); err != nil {
		t.Fatal(err)
	}
	s.processHarnessResults(p)
	row := s.harnessResults.Results()[0]
	b, err := json.Marshal(row)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Count(string(b), "House with trees.") != 1 || len(harnessResultReference(row)) != 64 {
		t.Fatal("duplicated result storage")
	}

}

func TestHarnessResultClaimFailurePreservesUIDeliveryForRetry(t *testing.T) {
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	dir := filepath.Join(root, "storage")
	s := &Server{agentHandler: &agenthttp.AgentHandler{}}
	if err := s.initializeHarnessResults(filepath.Join(dir, "results.json")); err != nil {
		t.Fatal(err)
	}
	p := harness.ResultContext{Owner: "owner", ServerInstanceID: "instance"}
	frame := harness.Frame{"type": "turn.send", "agentId": "agent", "idempotencyKey": "a", "text": "task"}
	s.registerHarnessDispatch("agent", "run-a", false, true, frame)
	if err := s.reserveHarnessResult(frame, p); err != nil {
		t.Fatal(err)
	}
	s.bindHarnessResultReceipt(serverReceipt("a"), p)
	if _, _, err := s.harnessResults.Apply(p.Owner, serverGroup("a"), time.Now()); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(dir, dir+"-moved"); err != nil {
		t.Fatal(err)
	}
	s.processHarnessResults(p)
	if !s.hasHarnessReply("agent", "run-a") || s.HarnessFollowupContext() != "" {
		t.Fatal("failed claim consumed UI route")
	}
	if err := os.Rename(dir+"-moved", dir); err != nil {
		t.Fatal(err)
	}
	s.processHarnessResults(p)
	if s.hasHarnessReply("agent", "run-a") || !strings.Contains(s.HarnessFollowupContext(), "House with trees.") {
		t.Fatal("storage recovery lost UI delivery")
	}
}
func TestHarnessResultStoreFailureDoesNotAcknowledgeEvent(t *testing.T) {
	s, p := resultServer(t)
	s.harnessResults = nil
	if err := s.captureHarnessResult(serverGroup("a"), p); err == nil {
		t.Fatal("event acknowledged without durable store")
	}
}

func TestHarnessResultRestorationIsDisplayOnly(t *testing.T) {
	capture := captureHarnessMetrics(t)
	s, _ := resultServer(t)
	for _, key := range []string{"a", "b"} {
		s.restoreHarnessResultRoute(harness.ResultInput{AgentID: "agent", RunID: "restored-" + key, IdempotencyKey: key, ResultID: "proven-result", Channel: "web", ExpiresAt: time.Now().Add(time.Minute)})
	}
	if s.harnessFollowup.Load() != 0 || s.HarnessFollowupContext() != "" || s.harnessOverlapAgents["agent"] {
		t.Fatal("restoration activated follow-up or overlap")
	}
	capture.mu.Lock()
	rows := len(capture.rows)
	capture.mu.Unlock()
	if rows != 0 {
		t.Fatal("restoration emitted dispatch telemetry")
	}
	for _, key := range []string{"a", "b"} {
		r := s.harnessReplies["restored-"+key]
		if !r.restored || r.overlapped {
			t.Fatal("restored route not display-only")
		}
	}
	s.registerHarnessDispatch("agent", "new-run", true, true, harness.Frame{"idempotencyKey": "new-key"})
	if s.harnessOverlapAgents["agent"] || s.harnessReplies["new-run"].overlapped {
		t.Fatal("restored display routes poisoned new-task overlap")
	}
	s.harnessRepliesMu.Lock()
	route, ok := s.harnessReplyForFrameLocked("agent", harness.Frame{})
	s.harnessRepliesMu.Unlock()
	if !ok || route.runID != "new-run" {
		t.Fatal("display-only routes blocked unambiguous legacy task")
	}
}
func TestHarnessResultRestorationPreservesLiveRoute(t *testing.T) {
	s, p := resultServer(t)
	reserveServerInput(t, s, p, "a")
	before := s.harnessReplies["run-a"]
	s.restoreHarnessResultRoute(harness.ResultInput{AgentID: "wrong-agent", RunID: "run-a", IdempotencyKey: "wrong", Channel: "voice", ExpiresAt: time.Now().Add(time.Hour)})
	if after := s.harnessReplies["run-a"]; after != before {
		t.Fatalf("current route overwritten: %+v", after)
	}
}
func TestHarnessResultRestartRestoresOnlyProvenDisplayOnce(t *testing.T) {
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "results.json")
	first := &Server{agentHandler: &agenthttp.AgentHandler{}}
	if err = first.initializeHarnessResults(path); err != nil {
		t.Fatal(err)
	}
	p := harness.ResultContext{Owner: "owner", MachineID: "machine", ServerInstanceID: "instance"}
	for _, key := range []string{"a", "b", "c"} {
		in := harness.ResultInput{Owner: p.Owner, ServerInstanceID: p.ServerInstanceID, AgentID: "agent", RunID: "run-" + key, IdempotencyKey: key, Channel: "voice", Destination: "voice:main:0", ExpiresAt: time.Now().Add(time.Minute)}
		if err = first.harnessResults.Reserve(in); err != nil {
			t.Fatal(err)
		}
		if err = first.harnessResults.BindReceipt(p.Owner, p.ServerInstanceID, "agent", key, "delivery-"+key); err != nil {
			t.Fatal(err)
		}
	}
	if err = first.captureHarnessResult(serverGroup("a", "b"), p); err != nil {
		t.Fatal(err)
	}
	restarted := &Server{agentHandler: &agenthttp.AgentHandler{}}
	if err = restarted.initializeHarnessResults(path); err != nil {
		t.Fatal(err)
	}
	if len(restarted.harnessReplies) != 0 {
		t.Fatal("initialization revived historical routes")
	}
	restarted.processHarnessResults(p)
	rows := restarted.harnessResults.Results()
	if len(rows) != 1 || rows[0].SpeechState != "suppressed" {
		t.Fatalf("restart spoke result: %+v", rows)
	}
	if !strings.Contains(restarted.HarnessFollowupContext(), "House with trees.") {
		t.Fatal("proven result not restored for display")
	}
	if len(restarted.harnessReplies) != 0 || restarted.harnessOverlapAgents["agent"] {
		t.Fatal("historical pending routes revived")
	}
	stamp := restarted.harnessResultAt
	followup := restarted.harnessFollowup.Load()
	if err = restarted.captureHarnessResult(serverGroup("b", "a"), p); err != nil {
		t.Fatal(err)
	}
	restarted.processHarnessResults(p)
	if !restarted.harnessResultAt.Equal(stamp) || restarted.harnessFollowup.Load() != followup || len(restarted.harnessReplies) != 0 {
		t.Fatal("replay republished result or revived consumed routes")
	}
}

func TestHarnessResultUnresolvedRestoredRouteStillProtectsNewTask(t *testing.T) {
	s, _ := resultServer(t)
	// This address belongs to a still-pending external history input. Unlike a
	// proven completed display result, it can still receive an old remote summary.
	s.restoreHarnessResultRoute(harness.ResultInput{AgentID: "agent", RunID: "waiting-a", IdempotencyKey: "a", Channel: "web", ExpiresAt: time.Now().Add(time.Minute)})
	s.registerHarnessDispatch("agent", "new-c", true, true, harness.Frame{"idempotencyKey": "c"})
	if !s.harnessOverlapAgents["agent"] || !s.harnessReplies["new-c"].overlapped {
		t.Fatal("pending restored input no longer protects against overlap")
	}
	s.harnessRepliesMu.Lock()
	_, ok := s.harnessReplyForFrameLocked("agent", harness.Frame{})
	s.harnessRepliesMu.Unlock()
	if ok {
		t.Fatal("uncorrelated old pending summary could consume new task")
	}
	s.forwardHarnessEvent(harness.Frame{"kind": "turn.summary", "agentId": "agent", "payload": map[string]any{"fullText": "old result"}})
	if !s.hasHarnessReply("agent", "new-c") || !s.hasHarnessReply("agent", "waiting-a") || s.HarnessFollowupContext() != "" {
		t.Fatal("ambiguous pending summary delivered after restart")
	}
}
