package opencode

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

func TestParseSlackInbound(t *testing.T) {
	cases := []struct {
		name        string
		body        string
		allowedUser string
		wantChall   string
		wantMsg     bool
		wantText    string
		wantUser    string
		wantChannel string
		wantThread  string
	}{
		{
			name:      "url_verification returns challenge",
			body:      `{"type":"url_verification","challenge":"abc123"}`,
			wantChall: "abc123",
		},
		{
			name:        "message starts a turn",
			body:        `{"type":"event_callback","event":{"type":"message","text":"hello there","user":"U1","channel":"C9","ts":"1.2"}}`,
			wantMsg:     true,
			wantText:    "hello there",
			wantUser:    "U1",
			wantChannel: "C9",
		},
		{
			name:        "app_mention strips leading mention",
			body:        `{"type":"event_callback","event":{"type":"app_mention","text":"<@U0BOT> ping","user":"U1","channel":"C9","thread_ts":"1.5"}}`,
			wantMsg:     true,
			wantText:    "ping",
			wantUser:    "U1",
			wantChannel: "C9",
			wantThread:  "1.5",
		},
		{
			name: "bot message ignored (loop guard)",
			body: `{"type":"event_callback","event":{"type":"message","text":"hi","user":"U1","bot_id":"B1","channel":"C9"}}`,
		},
		{
			name: "subtype ignored (edit/join)",
			body: `{"type":"event_callback","event":{"type":"message","subtype":"message_changed","text":"hi","user":"U1","channel":"C9"}}`,
		},
		{
			name: "empty user ignored",
			body: `{"type":"event_callback","event":{"type":"message","text":"hi","channel":"C9"}}`,
		},
		{
			name: "reaction event ignored (not a message)",
			body: `{"type":"event_callback","event":{"type":"reaction_added","user":"U1"}}`,
		},
		{
			name:        "disallowed user ignored",
			body:        `{"type":"event_callback","event":{"type":"message","text":"hi","user":"U2","channel":"C9"}}`,
			allowedUser: "U1",
		},
		{
			name:        "allowed user passes",
			body:        `{"type":"event_callback","event":{"type":"message","text":"hi","user":"U1","channel":"C9"}}`,
			allowedUser: "U1",
			wantMsg:     true,
			wantText:    "hi",
			wantUser:    "U1",
			wantChannel: "C9",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			chall, msg, err := parseSlackInbound(tc.body, tc.allowedUser)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if chall != tc.wantChall {
				t.Errorf("challenge = %q, want %q", chall, tc.wantChall)
			}
			if tc.wantMsg {
				if msg == nil {
					t.Fatalf("expected a message, got nil")
				}
				if msg.text != tc.wantText || msg.user != tc.wantUser || msg.channel != tc.wantChannel || msg.threadTS != tc.wantThread {
					t.Errorf("msg = %+v, want text=%q user=%q channel=%q thread=%q",
						*msg, tc.wantText, tc.wantUser, tc.wantChannel, tc.wantThread)
				}
			} else if msg != nil {
				t.Errorf("expected ignored (nil msg), got %+v", *msg)
			}
		})
	}
}

func TestParseSlackInboundBadJSON(t *testing.T) {
	if _, _, err := parseSlackInbound(`{not json`, ""); err == nil {
		t.Errorf("expected parse error for malformed body")
	}
}

func TestSlackRunMapRoundTrip(t *testing.T) {
	s := &OpenCodeService{slackRuns: make(map[string]slackRun)}

	s.markSlackRun("run-1", slackRun{channel: "C42", threadTS: "1.99", messageTS: "1.50"})
	if !s.IsSlackOriginRun("run-1") {
		t.Errorf("IsSlackOriginRun should be true before consume")
	}
	if !s.IsSlackOriginRun("run-1") {
		t.Errorf("IsSlackOriginRun should still be true (peek must not consume)")
	}
	o, ok := s.consumeSlackRun("run-1")
	if !ok || o.channel != "C42" || o.threadTS != "1.99" || o.messageTS != "1.50" {
		t.Fatalf("consumeSlackRun = (%+v,%v), want {C42,1.99,1.50},true", o, ok)
	}
	if s.IsSlackOriginRun("run-1") {
		t.Errorf("origin not cleared after consume")
	}
	if _, ok := s.consumeSlackRun("run-1"); ok {
		t.Errorf("second consume should miss")
	}
	if s.IsSlackOriginRun("nope") {
		t.Errorf("unknown run reported as slack origin")
	}
	s.markSlackRun("run-2", slackRun{})
	if s.IsSlackOriginRun("run-2") {
		t.Errorf("empty-channel origin should not be recorded")
	}
}

// slackAPICall is one captured Slack Web API request (method + JSON payload).
type slackAPICall struct {
	method  string
	payload map[string]any
}

// newFakeSlackAPI returns an httptest server that acks every Web API call with
// {"ok":true} and records each call on the returned channel.
func newFakeSlackAPI(t *testing.T) (*httptest.Server, chan slackAPICall) {
	t.Helper()
	calls := make(chan slackAPICall, 8)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var payload map[string]any
		_ = json.Unmarshal(body, &payload)
		calls <- slackAPICall{method: r.URL.Path, payload: payload}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	return srv, calls
}

// waitCall pops the next captured Slack API call or fails the test.
func waitCall(t *testing.T, calls chan slackAPICall) slackAPICall {
	t.Helper()
	select {
	case c := <-calls:
		return c
	case <-time.After(5 * time.Second):
		t.Fatal("no slack API call within 5s")
		return slackAPICall{}
	}
}

// TestHandleInboundSlack drives a forwarded Slack event through the bridge:
// the message must be injected as a turn with the sender-metadata prefix, the
// run tracked in slackRuns (thread_ts falling back to the message ts) and
// marked silent, and the eyes ack reaction added.
func TestHandleInboundSlack(t *testing.T) {
	srv, calls := newFakeSlackAPI(t)
	defer srv.Close()

	type sent struct{ text, reqID, runID string }
	injected := make(chan sent, 2)
	s := &OpenCodeService{
		config:       &config.Config{SlackUserID: "U1", SlackBotToken: "xoxb-test"},
		slackRuns:    make(map[string]slackRun),
		silentRuns:   make(map[string]bool),
		slackAPIBase: srv.URL,
	}
	s.slackSendTurn = func(text, reqID, runID string) error {
		injected <- sent{text: text, reqID: reqID, runID: runID}
		return nil
	}

	body := `{"type":"event_callback","event":{"type":"message","text":"hello lamp","user":"U1","channel":"C9","ts":"1.50"}}`
	challenge, handled, err := s.HandleInboundSlack(domain.SlackInbound{Body: body})
	if err != nil || challenge != "" || !handled {
		t.Fatalf("HandleInboundSlack = (%q, %v, %v), want (\"\", true, nil)", challenge, handled, err)
	}

	var got sent
	select {
	case got = <-injected:
	case <-time.After(5 * time.Second):
		t.Fatal("no turn injected within 5s")
	}
	want := "[slack] Message from <@U1> [channel:C9]:\nhello lamp"
	if got.text != want {
		t.Errorf("injected text = %q, want %q", got.text, want)
	}

	if !s.IsSilentRun(got.runID) {
		t.Errorf("run %q not marked silent — Slack replies must not hit TTS", got.runID)
	}
	ack := waitCall(t, calls)
	if ack.method != "/reactions.add" || ack.payload["name"] != "eyes" || ack.payload["timestamp"] != "1.50" {
		t.Errorf("ack call = %+v, want reactions.add eyes on ts 1.50", ack)
	}
	o, ok := s.consumeSlackRun(got.runID)
	if !ok || o.channel != "C9" || o.threadTS != "1.50" || o.messageTS != "1.50" {
		t.Errorf("slack run = (%+v,%v), want {C9,1.50,1.50},true (thread_ts falls back to message ts)", o, ok)
	}
}

// TestHandleInboundSlackIgnoredAndChallenge covers the no-turn outcomes:
// url_verification echoes the challenge; a bot message is intentionally
// ignored (handled == false, nothing injected).
func TestHandleInboundSlackIgnoredAndChallenge(t *testing.T) {
	s := &OpenCodeService{
		config:     &config.Config{},
		slackRuns:  make(map[string]slackRun),
		silentRuns: make(map[string]bool),
		slackSendTurn: func(string, string, string) error {
			t.Error("no turn must be injected for ignored events")
			return nil
		},
	}

	challenge, handled, err := s.HandleInboundSlack(domain.SlackInbound{Body: `{"type":"url_verification","challenge":"ch-77"}`})
	if err != nil || challenge != "ch-77" || handled {
		t.Errorf("url_verification = (%q, %v, %v), want (\"ch-77\", false, nil)", challenge, handled, err)
	}

	challenge, handled, err = s.HandleInboundSlack(domain.SlackInbound{
		Body: `{"type":"event_callback","event":{"type":"message","text":"hi","user":"U1","bot_id":"B1","channel":"C9"}}`,
	})
	if err != nil || challenge != "" || handled {
		t.Errorf("bot message = (%q, %v, %v), want (\"\", false, nil)", challenge, handled, err)
	}
}

// TestSlackReplyRoutingEmitFinal exercises the reply seam end-to-end: a
// Slack-marked run finishing via emitFinal must clear the eyes reaction and
// post the final text — [HW:/...] markers and audio tags stripped — to the
// originating channel/thread via chat.postMessage.
func TestSlackReplyRoutingEmitFinal(t *testing.T) {
	srv, calls := newFakeSlackAPI(t)
	defer srv.Close()

	s := &OpenCodeService{
		config:       &config.Config{SlackBotToken: "xoxb-test"},
		slackRuns:    make(map[string]slackRun),
		silentRuns:   make(map[string]bool),
		slackAPIBase: srv.URL,
	}
	runID := "device-chat-1-123"
	s.markSlackRun(runID, slackRun{channel: "C9", threadTS: "1.50", messageTS: "1.50"})
	s.setCurrentRunID(runID)
	s.assistantParts = []string{"[HW:/led/set:{\"r\":1}] Done! [laugh]"}

	s.emitFinal(func(domain.WSEvent) {})

	// Origin consumed synchronously (the handler's DeliverSlackReply safety
	// net must find nothing).
	if s.IsSlackOriginRun(runID) {
		t.Errorf("slack run not consumed by emitFinal")
	}

	first := waitCall(t, calls)
	if first.method != "/reactions.remove" || first.payload["name"] != "eyes" {
		t.Errorf("first call = %+v, want reactions.remove eyes", first)
	}
	post := waitCall(t, calls)
	if post.method != "/chat.postMessage" {
		t.Fatalf("second call = %+v, want chat.postMessage", post)
	}
	if post.payload["channel"] != "C9" || post.payload["thread_ts"] != "1.50" || post.payload["text"] != "Done!" {
		t.Errorf("postMessage payload = %+v, want channel C9, thread_ts 1.50, text %q (markers stripped)",
			post.payload, "Done!")
	}
}

// TestSlackRunConsumedOnError: a failed turn consumes the tracker (no leak),
// clears the ack reaction, and posts nothing.
func TestSlackRunConsumedOnError(t *testing.T) {
	var mu sync.Mutex
	var methods []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		methods = append(methods, r.URL.Path)
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()

	s := &OpenCodeService{
		config:       &config.Config{SlackBotToken: "xoxb-test"},
		slackRuns:    make(map[string]slackRun),
		silentRuns:   make(map[string]bool),
		slackAPIBase: srv.URL,
	}
	runID := "device-chat-2-456"
	s.markSlackRun(runID, slackRun{channel: "C9", threadTS: "1.50", messageTS: "1.50"})
	s.setCurrentRunID(runID)

	s.handleError("boom", func(domain.WSEvent) {})

	if s.IsSlackOriginRun(runID) {
		t.Errorf("slack run not consumed by handleError")
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		mu.Lock()
		n := len(methods)
		mu.Unlock()
		if n > 0 || time.Now().After(deadline) {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(methods) != 1 || methods[0] != "/reactions.remove" {
		t.Errorf("slack calls on error = %v, want exactly [/reactions.remove] (no reply post)", methods)
	}
}
