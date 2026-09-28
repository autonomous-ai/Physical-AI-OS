package server

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/lib/flow"
	agenthttp "go.autonomous.ai/os/system/server/agent/delivery/http"
	"go.autonomous.ai/os/system/server/config"
)

func TestHarnessRoutesProtectCodeAndLocalCommands(t *testing.T) {
	service, err := harness.NewService(t.TempDir(), harness.Callbacks{})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	service.Start(ctx)
	s := &Server{config: &config.Config{LLMAPIKey: "test-owner-token"}, harnessService: service}
	router := gin.New()
	s.registerHarnessRoutes(router.Group("/api"), ctx)
	call := func(method, path, address, token, forwarded string) *httptest.ResponseRecorder {
		t.Helper()
		req := httptest.NewRequest(method, path, nil)
		req.RemoteAddr = address
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		if forwarded != "" {
			req.Header.Set("X-Forwarded-For", forwarded)
		}
		out := httptest.NewRecorder()
		router.ServeHTTP(out, req)
		return out
	}
	for _, path := range []string{"/api/harness/pair", "/api/harness/pair/status"} {
		method := http.MethodGet
		if path == "/api/harness/pair" {
			method = http.MethodPost
		}
		if out := call(method, path, "192.168.1.20:1234", "", ""); out.Code != http.StatusUnauthorized {
			t.Fatalf("unauthorized %s: %d", path, out.Code)
		}
	}
	out := call(http.MethodPost, "/api/harness/pair", "192.168.1.20:1234", "test-owner-token", "")
	if out.Code != http.StatusAccepted || out.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("generate without machine ID: %d %s", out.Code, out.Body.String())
	}
	var generated struct {
		Data harness.PairInfo `json:"data"`
	}
	if err := json.Unmarshal(out.Body.Bytes(), &generated); err != nil || len(generated.Data.Code) != 6 {
		t.Fatalf("device must generate the code: %v", err)
	}
	out = call(http.MethodGet, "/api/harness/status", "127.0.0.1:1234", "", "")
	var status struct {
		Data map[string]any `json:"data"`
	}
	if err := json.Unmarshal(out.Body.Bytes(), &status); err != nil || out.Code != http.StatusOK {
		t.Fatalf("local status: %d %v", out.Code, err)
	}
	if _, leaked := status.Data["code"]; leaked {
		t.Fatal("public connection status leaked pairing code")
	}
	for _, path := range []string{"/api/harness/request", "/api/harness/select-agent"} {
		for _, address := range []string{"192.168.1.20:1234", "127.0.0.1:1234"} {
			out = call(http.MethodPost, path, address, "test-owner-token", "192.168.1.20")
			if out.Code != http.StatusForbidden {
				t.Fatalf("LAN/proxied request admitted at %s: %d", path, out.Code)
			}
		}
	}
	disabled := false
	s.config.JevHarness = &config.JevIntentConfig{Enabled: &disabled}
	req := httptest.NewRequest(http.MethodPost, "/api/harness/select-agent", strings.NewReader(`{"machineId":"mac","agentId":"house","text":"Add trees"}`))
	req.RemoteAddr = "127.0.0.1:1234"
	selected := httptest.NewRecorder()
	router.ServeHTTP(selected, req)
	var selection struct {
		Data harnessSelection `json:"data"`
	}
	if err := json.Unmarshal(selected.Body.Bytes(), &selection); err != nil || selected.Code != http.StatusOK || selection.Data.Mode != "disabled" || selection.Data.AgentID != "house" {
		t.Fatalf("disabled selector did not retain original target: %d %s", selected.Code, selected.Body)
	}
	out = call(http.MethodPost, "/api/harness/pair/cancel", "192.168.1.20:1234", "test-owner-token", "")
	if out.Code != http.StatusOK || service.PairStatus().Code != "" {
		t.Fatalf("cancel did not clear code: %d", out.Code)
	}
}

func TestExtractHarnessReplyIsLocalRoutingOnly(t *testing.T) {
	frame := harness.Frame{
		"type":     "turn.send",
		"response": map[string]any{"run_id": "device-chat-42", "channel": "web"},
	}
	reply, err := extractHarnessReply(frame)
	if err != nil {
		t.Fatal(err)
	}
	if reply == nil || reply.RunID != "device-chat-42" || reply.Channel != "web" {
		t.Fatalf("reply = %#v", reply)
	}
	if _, sentToHarness := frame["response"]; sentToHarness {
		t.Fatal("local response routing reached Harness frame")
	}
}

func TestCanonicalHarnessReplyRunIDRepairsStaleSequence(t *testing.T) {
	const current = "device-chat-68-1789005100949"
	flow.Log("sensing_input", map[string]any{"type": "web_chat"}, current)
	if got := canonicalHarnessReplyRunID("device-chat-60-1789005100949"); got != current {
		t.Fatalf("canonical route = %q, want %q", got, current)
	}
	if got := canonicalHarnessReplyRunID("device-chat-60-1789005100999"); got != "device-chat-60-1789005100999" {
		t.Fatalf("different timestamp was rewritten: %q", got)
	}
}

func TestHarnessEventTextUsesDirectLifecycleAndSummary(t *testing.T) {
	if got := harnessEventText("receipt.updated", harness.Frame{
		"payload": map[string]any{"receipt": map[string]any{"state": "queued"}},
	}); got != "Harness queued the request." {
		t.Fatalf("queued text = %q", got)
	}
	if got := harnessEventText("turn.started", harness.Frame{"payload": map[string]any{}}); got != "Harness agent is working." {
		t.Fatalf("started text = %q", got)
	}
	if got := harnessEventText("turn.summary", harness.Frame{
		"payload": map[string]any{"fullText": "Complete Harness answer\n\n- one\n- two", "text": "Exact Harness answer", "recap": "short recap"},
	}); got != "Complete Harness answer\n\n- one\n- two" {
		t.Fatalf("summary text = %q", got)
	}
	if got := harnessRecapResultText(harness.Frame{
		"turns": []any{map[string]any{"fullText": "The complete Harness answer, including every recommendation.", "text": "Short preview"}},
	}); got != "The complete Harness answer, including every recommendation." {
		t.Fatalf("recap result text = %q", got)
	}
	if got := harnessRecapResultText(harness.Frame{
		"turns": []any{map[string]any{"recap": "short label"}},
	}); got != "" {
		t.Fatalf("missing recap text = %q", got)
	}
	name, args := harnessToolEvent(harness.Frame{
		"payload": map[string]any{"text": "web_search", "detail": "US events September"},
	})
	if name != "web_search" || args != "US events September" {
		t.Fatalf("tool event = %q, %q", name, args)
	}
	if got := harnessEventText("question.open", harness.Frame{
		"payload": map[string]any{"questions": []any{map[string]any{"question": "Which city should I use?"}}},
	}); got != "Which city should I use?" {
		t.Fatalf("question text = %q", got)
	}
}

func TestForgetHarnessReplyOnlyRemovesMatchingRun(t *testing.T) {
	s := &Server{harnessReplies: map[string]harnessReply{
		"current": {agentID: "agent-1", runID: "current", created: time.Now()},
	}}
	s.forgetHarnessReply("agent-1", "older")
	if _, ok := s.harnessReplies["current"]; !ok {
		t.Fatal("an older failed request removed the current reply route")
	}
	s.forgetHarnessReply("agent-1", "current")
	if _, ok := s.harnessReplies["current"]; ok {
		t.Fatal("matching failed request left its reply route behind")
	}
}

func TestHarnessFollowupContextExpiresWithFollowupWindow(t *testing.T) {
	s := &Server{}
	s.rememberHarnessResult("house-agent", "house-run", "Harness found two restaurants.")
	if got := s.HarnessFollowupContext(); !strings.Contains(got, `"agentId":"house-agent"`) || !strings.Contains(got, `"responseRunId":"house-run"`) || !strings.Contains(got, "Harness found two restaurants.") {
		t.Fatalf("follow-up context = %q", got)
	}
	s.harnessFollowup.Store(time.Now().Add(-time.Second).UnixMilli())
	if got := s.HarnessFollowupContext(); got != "" {
		t.Fatalf("expired follow-up context = %q", got)
	}
}

func TestHarnessFollowupResultKeepsMatchingProvenance(t *testing.T) {
	s := &Server{}
	s.rememberHarnessResult("airplane-agent", "airplane-run", "Airplane ready")
	s.rememberHarnessResult("house-agent", "house-run", "House ready\nQuoted data: \"agentId\":\"airplane-agent\"")
	var result struct {
		AgentID string `json:"agentId"`
		RunID   string `json:"responseRunId"`
		Text    string `json:"text"`
	}
	if err := json.Unmarshal([]byte(s.HarnessFollowupContext()), &result); err != nil {
		t.Fatal(err)
	}
	if result.AgentID != "house-agent" || result.RunID != "house-run" || !strings.HasPrefix(result.Text, "House ready") {
		t.Fatalf("result crossed task provenance: %+v", result)
	}
}

func TestHarnessSummaryPersistsCompleteChatResult(t *testing.T) {
	const runID = "device-chat-summary-recovery"
	const fullText = "Kết quả đầy đủ\n\n- Mục một\n- Mục hai"
	s := &Server{agentHandler: &agenthttp.AgentHandler{}}
	s.registerHarnessReply("mike", runID, true, false)
	s.forwardHarnessEvent(harness.Frame{"agentId": "mike", "kind": "turn.done", "payload": map[string]any{}})
	if _, pending := s.harnessReplies[runID]; !pending {
		t.Fatal("turn.done consumed final route")
	}
	s.forwardHarnessEvent(harness.Frame{"agentId": "mike", "kind": "turn.summary", "payload": map[string]any{"text": "preview", "fullText": fullText}})
	count := 0
	for _, event := range flow.Recent(100) {
		if event.Node == "harness_response" && event.TraceID == runID {
			count++
			if event.Data["text"] != fullText {
				t.Fatalf("recovery lost full text: %#v", event.Data)
			}
		}
	}
	if count != 1 {
		t.Fatalf("want one recoverable final, got %d", count)
	}
	if _, pending := s.harnessReplies[runID]; pending {
		t.Fatal("summary did not finish route")
	}
}

func TestHarnessEventFromAnotherAgentCannotFinishPendingChat(t *testing.T) {
	s := &Server{agentHandler: &agenthttp.AgentHandler{}}
	s.registerHarnessReply("mike", "device-chat-mike", true, false)
	s.forwardHarnessEvent(harness.Frame{"agentId": "other-agent", "kind": "turn.summary", "payload": map[string]any{"text": "unrelated answer"}})
	if _, pending := s.harnessReplies["device-chat-mike"]; !pending {
		t.Fatal("unrelated agent consumed Mike's pending chat")
	}
}

func TestEmptyHarnessSummaryRetainsPendingChat(t *testing.T) {
	s := &Server{agentHandler: &agenthttp.AgentHandler{}}
	s.registerHarnessReply("mike", "device-chat-empty-summary", true, false)
	s.forwardHarnessEvent(harness.Frame{"agentId": "mike", "kind": "turn.summary", "payload": map[string]any{}})
	if _, pending := s.harnessReplies["device-chat-empty-summary"]; !pending {
		t.Fatal("empty summary consumed pending chat")
	}
	s.forwardHarnessEvent(harness.Frame{"agentId": "mike", "kind": "turn.summary", "payload": map[string]any{"text": "complete answer"}})
	if _, pending := s.harnessReplies["device-chat-empty-summary"]; pending {
		t.Fatal("nonempty summary did not complete pending chat")
	}
}

func TestHarnessSameAgentKeepsEachChatRouteUntilItsOwnSummary(t *testing.T) {
	s := &Server{agentHandler: &agenthttp.AgentHandler{}}
	s.registerHarnessDispatch("mike", "device-chat-first", true, false, harness.Frame{"idempotencyKey": "first-key"})
	s.registerHarnessDispatch("mike", "device-chat-second", true, false, harness.Frame{"idempotencyKey": "second-key"})
	s.forwardHarnessEvent(harness.Frame{"agentId": "mike", "kind": "turn.summary", "payload": map[string]any{"idempotencyKey": "first-key", "text": "first result"}})
	if _, pending := s.harnessReplies["device-chat-first"]; pending {
		t.Fatal("first result did not consume the first route")
	}
	if _, pending := s.harnessReplies["device-chat-second"]; !pending {
		t.Fatal("first result consumed the newer route")
	}
	s.forwardHarnessEvent(harness.Frame{"agentId": "mike", "kind": "turn.summary", "payload": map[string]any{"idempotencyKey": "second-key", "text": "second result"}})
	if _, pending := s.harnessReplies["device-chat-second"]; pending {
		t.Fatal("second result did not consume the second route")
	}
}

func TestHarnessOverlappingRepliesRejectUncorrelatedAndLateEvents(t *testing.T) {
	s := &Server{agentHandler: &agenthttp.AgentHandler{}}
	s.registerHarnessDispatch("agent", "first", true, false, harness.Frame{"idempotencyKey": "key-a"})
	s.registerHarnessDispatch("agent", "second", true, false, harness.Frame{"idempotencyKey": "key-b"})
	for _, payload := range []map[string]any{{"text": "ambiguous"}, {"text": "unknown", "idempotencyKey": "unknown"}, {"text": "mismatch", "idempotencyKey": "key-a", "runId": "second"}} {
		s.forwardHarnessEvent(harness.Frame{"agentId": "agent", "kind": "turn.summary", "payload": payload})
		if len(s.harnessReplies) != 2 {
			t.Fatal("unmatched event consumed a route")
		}
	}
	s.forwardHarnessEvent(harness.Frame{"agentId": "agent", "kind": "turn.summary", "payload": map[string]any{"idempotencyKey": "key-b", "text": "preview", "fullText": "second complete"}})
	if !s.hasHarnessReply("agent", "first") || s.hasHarnessReply("agent", "second") {
		t.Fatal("out-of-order completion reached wrong route")
	}
	s.forwardHarnessEvent(harness.Frame{"agentId": "agent", "kind": "turn.summary", "payload": map[string]any{"text": "late legacy second result"}})
	if !s.hasHarnessReply("agent", "first") {
		t.Fatal("late ambiguous event consumed remaining route")
	}
	if !s.harnessReplies["first"].overlapped {
		t.Fatal("remaining route lost overlap evidence")
	}
}

func TestHarnessReceiptEventCorrelatesByNestedKey(t *testing.T) {
	s := &Server{agentHandler: &agenthttp.AgentHandler{}}
	s.registerHarnessDispatch("agent", "first", true, false, harness.Frame{"idempotencyKey": "key-a"})
	s.registerHarnessDispatch("agent", "second", true, false, harness.Frame{"idempotencyKey": "key-b"})
	reply, ok := s.harnessReplyForFrameLocked("agent", harness.Frame{"payload": map[string]any{"receipt": map[string]any{"idempotencyKey": "key-b", "state": "queued"}}})
	if !ok || reply.runID != "second" {
		t.Fatal("receipt routed to wrong turn")
	}
	if _, ok := s.harnessReplyForEventLocked("agent", "unknown"); ok {
		t.Fatal("unknown explicit run fell back")
	}
}

func TestHarnessLocalNoticeDoesNotPoisonPendingRemoteReply(t *testing.T) {
	s := &Server{agentHandler: &agenthttp.AgentHandler{}}
	s.registerHarnessDispatch("agent", "remote", true, false, harness.Frame{"idempotencyKey": "key-a"})
	s.deliverHarnessVoiceMessage("agent", "local-failure", "Harness focus changed")
	if !s.hasHarnessReply("agent", "remote") || s.harnessReplies["remote"].overlapped || s.harnessOverlapAgents["agent"] {
		t.Fatal("local failure was mistaken for overlapping remote execution")
	}
	s.forwardHarnessEvent(harness.Frame{"agentId": "agent", "kind": "turn.summary", "payload": map[string]any{"fullText": "remote result"}})
	if s.hasHarnessReply("agent", "remote") {
		t.Fatal("local failure blocked the existing remote result")
	}
}

func TestHarnessLateLegacyEventCannotCompleteFutureTurnAfterOverlap(t *testing.T) {
	s := &Server{agentHandler: &agenthttp.AgentHandler{}}
	s.registerHarnessReply("agent", "a", true, false)
	s.registerHarnessReply("agent", "b", true, false)
	s.takeHarnessReply("agent", "a")
	s.takeHarnessReply("agent", "b")
	s.registerHarnessReply("agent", "c", true, false)
	if _, ok := s.harnessReplyForEventLocked("agent", ""); ok {
		t.Fatal("uncorrelated late event can complete future turn")
	}
	if _, ok := s.harnessReplyForEventLocked("agent", "c"); !ok {
		t.Fatal("explicit future turn lost")
	}
}
