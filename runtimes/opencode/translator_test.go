package opencode

import (
	"encoding/json"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

// collectEvents drives translateFrame over a sequence of raw JSONL lines and
// returns every dispatched domain.WSEvent in order.
func collectEvents(t *testing.T, s *OpenCodeService, lines []string) []domain.WSEvent {
	t.Helper()
	var got []domain.WSEvent
	dispatch := func(e domain.WSEvent) { got = append(got, e) }
	for _, l := range lines {
		s.translateFrame([]byte(l), dispatch)
	}
	return got
}

// agentPhase returns (stream, phase) for an agent WSEvent, else "","".
func agentPhase(e domain.WSEvent) (stream, phase string) {
	if e.Event != "agent" {
		return "", ""
	}
	var p struct {
		Stream string `json:"stream"`
		Data   struct {
			Phase string `json:"phase"`
		} `json:"data"`
	}
	_ = json.Unmarshal(e.Payload, &p)
	return p.Stream, p.Data.Phase
}

// TestTranslateHappyTurn: a full opencode run turn maps to the OpenClaw WSEvent
// contract — lifecycle.start, tool.start/end, assistant delta(final), chat.final,
// lifecycle.end(usage) — and the session key + final text are captured.
func TestTranslateHappyTurn(t *testing.T) {
	s := &OpenCodeService{}
	lines := []string{
		`{"type":"step_start","sessionID":"sess-1"}`,
		`{"type":"text","sessionID":"sess-1","text":"Hello"}`,
		`{"type":"tool_use","sessionID":"sess-1","id":"tc1","tool":"bash","input":{"cmd":"ls"}}`,
		`{"type":"reasoning","sessionID":"sess-1","text":"thinking..."}`,
		`{"type":"text","sessionID":"sess-1","text":"world"}`,
		`{"type":"message.updated","sessionID":"sess-1","info":{"tokens":{"input":10,"output":5,"cache":{"read":2,"write":0}}}}`,
		`{"type":"session.idle","sessionID":"sess-1"}`,
	}
	got := collectEvents(t, s, lines)

	if s.GetSessionKey() != "sess-1" {
		t.Fatalf("session key not captured: %q", s.GetSessionKey())
	}

	// Expected agent streams in order: lifecycle start, tool start, tool end,
	// assistant delta, (chat.final is an Event=chat), lifecycle end.
	var lifecycleStart, toolStart, toolEnd, assistantDelta, lifecycleEnd bool
	var chatFinal string
	var lifecycleEndUsage *domain.TokenUsage
	for _, e := range got {
		if e.Event == "chat" {
			var c struct {
				State   string `json:"state"`
				Message string `json:"message"`
			}
			_ = json.Unmarshal(e.Payload, &c)
			if c.State == "final" {
				chatFinal = c.Message
			}
			continue
		}
		stream, phase := agentPhase(e)
		switch {
		case stream == "lifecycle" && phase == "start":
			lifecycleStart = true
		case stream == "lifecycle" && phase == "end":
			lifecycleEnd = true
			var p struct {
				Data struct {
					Usage *domain.TokenUsage `json:"usage"`
				} `json:"data"`
			}
			_ = json.Unmarshal(e.Payload, &p)
			lifecycleEndUsage = p.Data.Usage
		case stream == "tool" && phase == "start":
			toolStart = true
		case stream == "tool" && phase == "end":
			toolEnd = true
		case stream == "assistant":
			assistantDelta = true
		}
	}

	if !lifecycleStart || !toolStart || !toolEnd || !assistantDelta || !lifecycleEnd {
		t.Fatalf("missing expected events: start=%v toolStart=%v toolEnd=%v delta=%v end=%v",
			lifecycleStart, toolStart, toolEnd, assistantDelta, lifecycleEnd)
	}
	if chatFinal != "world" {
		t.Fatalf("final chat text = %q, want %q", chatFinal, "world")
	}
	if lifecycleEndUsage == nil || lifecycleEndUsage.InputTokens != 10 ||
		lifecycleEndUsage.CacheReadTokens != 2 || lifecycleEndUsage.OutputTokens != 5 ||
		lifecycleEndUsage.TotalTokens != 17 {
		t.Fatalf("usage not carried to lifecycle.end: %+v", lifecycleEndUsage)
	}
}

// TestTranslateDeviceShape: the device-verified opencode 1.18.4 turn shape —
// text.part.text for the reply, step_finish.part.tokens for usage, and the
// gatewayd-synthesized session.idle as terminal — yields the reply + usage.
func TestTranslateDeviceShape(t *testing.T) {
	s := &OpenCodeService{}
	lines := []string{
		`{"type":"step_start","sessionID":"s1"}`,
		`{"type":"text","sessionID":"s1","part":{"text":"OPENCODE_OK"}}`,
		`{"type":"step_finish","sessionID":"s1","part":{"reason":"stop","tokens":{"input":14000,"output":5,"cache":{"read":100,"write":0}}}}`,
		`{"type":"session.idle","sessionID":"s1"}`,
	}
	got := collectEvents(t, s, lines)
	var chatFinal string
	var usage *domain.TokenUsage
	for _, e := range got {
		if e.Event == "chat" {
			var c struct{ State, Message string }
			_ = json.Unmarshal(e.Payload, &c)
			if c.State == "final" {
				chatFinal = c.Message
			}
		}
		if stream, phase := agentPhase(e); stream == "lifecycle" && phase == "end" {
			var p struct {
				Data struct {
					Usage *domain.TokenUsage `json:"usage"`
				} `json:"data"`
			}
			_ = json.Unmarshal(e.Payload, &p)
			usage = p.Data.Usage
		}
	}
	if chatFinal != "OPENCODE_OK" {
		t.Fatalf("reply not delivered from part.text: %q", chatFinal)
	}
	if usage == nil || usage.InputTokens != 14000 || usage.CacheReadTokens != 100 ||
		usage.OutputTokens != 5 || usage.TotalTokens != 14105 {
		t.Fatalf("usage not captured from step_finish.part.tokens: %+v", usage)
	}
}

// TestTranslateSessionError: a session.error frame ends the turn with lifecycle.error.
func TestTranslateSessionError(t *testing.T) {
	s := &OpenCodeService{}
	lines := []string{
		`{"type":"step_start","sessionID":"sess-2"}`,
		`{"type":"session.error","sessionID":"sess-2","error":{"message":"provider auth failed"}}`,
	}
	got := collectEvents(t, s, lines)

	sawError := false
	for _, e := range got {
		if stream, phase := agentPhase(e); stream == "lifecycle" && phase == "error" {
			sawError = true
			var p struct {
				Data struct {
					Error string `json:"error"`
				} `json:"data"`
			}
			_ = json.Unmarshal(e.Payload, &p)
			if p.Data.Error != "provider auth failed" {
				t.Fatalf("lifecycle.error message = %q", p.Data.Error)
			}
		}
	}
	if !sawError {
		t.Fatalf("expected a lifecycle.error event, got %d events", len(got))
	}
}

// TestTranslateTextFallbackToPart: when text rides a nested "part.text" instead
// of the flat "text" field, it is still accumulated.
func TestTranslateTextFallbackToPart(t *testing.T) {
	s := &OpenCodeService{}
	lines := []string{
		`{"type":"step_start","sessionID":"s"}`,
		`{"type":"text","sessionID":"s","part":{"text":"nested reply"}}`,
		`{"type":"session.idle","sessionID":"s"}`,
	}
	got := collectEvents(t, s, lines)
	var chatFinal string
	for _, e := range got {
		if e.Event == "chat" {
			var c struct {
				State, Message string
			}
			_ = json.Unmarshal(e.Payload, &c)
			if c.State == "final" {
				chatFinal = c.Message
			}
		}
	}
	if chatFinal != "nested reply" {
		t.Fatalf("nested part.text not accumulated: %q", chatFinal)
	}
}
