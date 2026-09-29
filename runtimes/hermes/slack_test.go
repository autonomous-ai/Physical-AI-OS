package hermes

import "testing"

func TestParseSlackInbound(t *testing.T) {
	cases := []struct {
		name        string
		body        string
		allowedUser string
		wantChall   string
		wantMsg     bool
		wantText    string
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
			wantChannel: "C9",
		},
		{
			name:        "app_mention strips leading mention",
			body:        `{"type":"event_callback","event":{"type":"app_mention","text":"<@U0BOT> ping","user":"U1","channel":"C9","thread_ts":"1.5"}}`,
			wantMsg:     true,
			wantText:    "ping",
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
				if msg.text != tc.wantText || msg.channel != tc.wantChannel || msg.threadTS != tc.wantThread {
					t.Errorf("msg = %+v, want text=%q channel=%q thread=%q", *msg, tc.wantText, tc.wantChannel, tc.wantThread)
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

func TestSlackOriginMapRoundTrip(t *testing.T) {
	s := &HermesService{slackRunOrigin: make(map[string]slackOrigin)}

	s.markSlackOrigin("run-1", "C42", "1.99", "1.50")
	if !s.IsSlackOriginRun("run-1") {
		t.Errorf("IsSlackOriginRun should be true before consume")
	}
	if !s.IsSlackOriginRun("run-1") {
		t.Errorf("IsSlackOriginRun should still be true (peek must not consume)")
	}
	o, ok := s.consumeSlackOrigin("run-1")
	if !ok || o.channel != "C42" || o.threadTS != "1.99" || o.messageTS != "1.50" {
		t.Fatalf("consumeSlackOrigin = (%+v,%v), want {C42,1.99,1.50},true", o, ok)
	}
	if s.IsSlackOriginRun("run-1") {
		t.Errorf("origin not cleared after consume")
	}
	if _, ok := s.consumeSlackOrigin("run-1"); ok {
		t.Errorf("second consume should miss")
	}
	if s.IsSlackOriginRun("nope") {
		t.Errorf("unknown run reported as slack origin")
	}
	s.markSlackOrigin("run-2", "", "", "")
	if s.IsSlackOriginRun("run-2") {
		t.Errorf("empty-channel origin should not be recorded")
	}
}
