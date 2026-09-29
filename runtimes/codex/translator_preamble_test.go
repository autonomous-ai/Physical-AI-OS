package codex

import (
	"encoding/json"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

// capture collects the text dispatched on each stream so a test can assert what
// reached the reply (spoken) versus what was demoted to thinking (Flow Monitor).
type capture struct {
	assistant []string
	thinking  []string
}

func (c *capture) dispatch(e domain.WSEvent) {
	var p struct {
		Stream string `json:"stream"`
		Data   struct {
			Delta string `json:"delta"`
			Text  string `json:"text"`
		} `json:"data"`
	}
	if json.Unmarshal(e.Payload, &p) != nil {
		return
	}
	text := p.Data.Delta
	if text == "" {
		text = p.Data.Text
	}
	if text == "" {
		return
	}
	switch p.Stream {
	case "assistant":
		c.assistant = append(c.assistant, text)
	case "thinking":
		c.thinking = append(c.thinking, text)
	}
}

func agentMessage(text string) []byte {
	raw, _ := json.Marshal(map[string]any{
		"type": "item.completed",
		"item": map[string]any{"item_type": "agent_message", "text": text},
	})
	return raw
}

// Codex exec narrates before each tool call.
// Only the last agent_message is the reply — the preambles must not be joined into the spoken
// text.
func TestPreamblesDoNotReachReply(t *testing.T) {
	cases := []struct {
		name  string
		parts []string
		want  string
	}{
		{
			name: "presence.enter",
			parts: []string{
				"Using the sensing skill for this presence event.",
				"Oh — hi. I don't think we've met yet.",
			},
			want: "Oh — hi. I don't think we've met yet.",
		},
		{
			name: "motion.activity posture nudge",
			parts: []string{
				"Posture summary is present, so this is the posture-nudge route.",
				"Posture-nudge route confirmed (route #5). Checking the emotion marker format.",
				"Sitting close to that screen a while — your neck's been taking the weight. [calm]",
			},
			want: "Sitting close to that screen a while — your neck's been taking the weight. [calm]",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := &CodexService{}
			c := &capture{}
			for _, part := range tc.parts {
				s.translateFrame(agentMessage(part), c.dispatch)
			}
			s.emitFinal(codexFrame{}, c.dispatch)

			final := strings.Join(c.assistant, "")
			if final != tc.want {
				t.Errorf("reply = %q, want %q", final, tc.want)
			}
			if len(c.thinking) != len(tc.parts)-1 {
				t.Errorf("thinking count = %d, want %d (%q)", len(c.thinking), len(tc.parts)-1, c.thinking)
			}
		})
	}
}

// A non-final agent_message carrying a [HW:…] marker is a real hardware action,
// not narration — it stays in the reply so the marker still fires.
func TestPreambleWithHWMarkerIsKept(t *testing.T) {
	s := &CodexService{}
	c := &capture{}
	s.translateFrame(agentMessage("[HW:/led/on] Getting the light on for you."), c.dispatch)
	s.translateFrame(agentMessage("All set."), c.dispatch)
	s.emitFinal(codexFrame{}, c.dispatch)

	final := strings.Join(c.assistant, "")
	if !strings.Contains(final, "[HW:/led/on]") {
		t.Errorf("reply lost the hardware marker: %q", final)
	}
	if !strings.Contains(final, "All set.") {
		t.Errorf("reply lost the final message: %q", final)
	}
	if len(c.thinking) != 0 {
		t.Errorf("marker-bearing part was demoted to thinking: %q", c.thinking)
	}
}

// A single-message turn (the common case) is unchanged.
func TestSingleAgentMessageUnchanged(t *testing.T) {
	s := &CodexService{}
	c := &capture{}
	s.translateFrame(agentMessage("Hello there."), c.dispatch)
	s.emitFinal(codexFrame{}, c.dispatch)

	if final := strings.Join(c.assistant, ""); final != "Hello there." {
		t.Errorf("reply = %q, want %q", final, "Hello there.")
	}
	if len(c.thinking) != 0 {
		t.Errorf("unexpected thinking: %q", c.thinking)
	}
}

// Codex speaks the OpenAI Responses API: `input_tokens` already includes
// `cached_input_tokens`.
// Adding them (as this did before campaign-api enabled prompt caching, when cached was always 0 and
// the bug was invisible) reports double the context and halves the effective rotation threshold.
func TestCodexUsageSubtractsCachedFromInput(t *testing.T) {
	u := &codexUse{InputTokens: 17623, CachedInputTokens: 17408, OutputTokens: 31}
	got := u.toDomain()
	if got.InputTokens != 215 {
		t.Fatalf("fresh input = %d, want 215 (17623 - 17408)", got.InputTokens)
	}
	if got.CacheReadTokens != 17408 {
		t.Fatalf("cache read = %d, want 17408", got.CacheReadTokens)
	}
	if got.TotalTokens != 17654 {
		t.Fatalf("total = %d, want 17654 (215 + 17408 + 31)", got.TotalTokens)
	}
}

// No caching (every turn before the campaign-api fix) must still report the
// full prompt as fresh input.
func TestCodexUsageWithoutCacheIsUnchanged(t *testing.T) {
	got := (&codexUse{InputTokens: 17623, OutputTokens: 31}).toDomain()
	if got.InputTokens != 17623 || got.CacheReadTokens != 0 || got.TotalTokens != 17654 {
		t.Fatalf("uncached usage mangled: %+v", got)
	}
}
