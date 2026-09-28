package opencode

import (
	"encoding/json"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

// streamTexts returns the text dispatched on one agent stream, in order, so a
// test can assert what reached the reply (spoken) versus what was demoted to
// thinking (Flow Monitor only).
func streamTexts(events []domain.WSEvent, stream string) []string {
	var out []string
	for _, e := range events {
		if e.Event != "agent" {
			continue
		}
		var p struct {
			Stream string `json:"stream"`
			Data   struct {
				Delta string `json:"delta"`
				Text  string `json:"text"`
			} `json:"data"`
		}
		if json.Unmarshal(e.Payload, &p) != nil || p.Stream != stream {
			continue
		}
		if text := p.Data.Delta; text != "" {
			out = append(out, text)
		} else if p.Data.Text != "" {
			out = append(out, p.Data.Text)
		}
	}
	return out
}

func textFrame(text string) string {
	raw, _ := json.Marshal(map[string]any{
		"type": "text", "sessionID": "sess-1", "text": text,
	})
	return string(raw)
}

// opencode narrates before each tool call as its own text part.
// Only the last part is the reply — the preambles must not be joined into the spoken text.
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
			lines := []string{`{"type":"step_start","sessionID":"sess-1"}`}
			for _, part := range tc.parts {
				lines = append(lines, textFrame(part))
			}
			lines = append(lines, `{"type":"session.idle","sessionID":"sess-1"}`)

			got := collectEvents(t, &OpenCodeService{}, lines)

			if reply := strings.Join(streamTexts(got, "assistant"), ""); reply != tc.want {
				t.Errorf("reply = %q, want %q", reply, tc.want)
			}
			if thinking := streamTexts(got, "thinking"); len(thinking) != len(tc.parts)-1 {
				t.Errorf("thinking count = %d, want %d (%q)", len(thinking), len(tc.parts)-1, thinking)
			}
		})
	}
}

// A non-final text part carrying a [HW:…] marker is a real hardware action, not
// narration — it stays in the reply so the marker still fires.
func TestPreambleWithHWMarkerIsKept(t *testing.T) {
	got := collectEvents(t, &OpenCodeService{}, []string{
		`{"type":"step_start","sessionID":"sess-1"}`,
		textFrame("[HW:/led/on] Getting the light on for you."),
		textFrame("All set."),
		`{"type":"session.idle","sessionID":"sess-1"}`,
	})

	reply := strings.Join(streamTexts(got, "assistant"), "")
	if !strings.Contains(reply, "[HW:/led/on]") {
		t.Errorf("reply lost the hardware marker: %q", reply)
	}
	if !strings.Contains(reply, "All set.") {
		t.Errorf("reply lost the final part: %q", reply)
	}
	if thinking := streamTexts(got, "thinking"); len(thinking) != 0 {
		t.Errorf("marker-bearing part was demoted to thinking: %q", thinking)
	}
}

// A single-part turn (the common case) is unchanged.
func TestSingleTextPartUnchanged(t *testing.T) {
	got := collectEvents(t, &OpenCodeService{}, []string{
		`{"type":"step_start","sessionID":"sess-1"}`,
		textFrame("Hello there."),
		`{"type":"session.idle","sessionID":"sess-1"}`,
	})

	if reply := strings.Join(streamTexts(got, "assistant"), ""); reply != "Hello there." {
		t.Errorf("reply = %q, want %q", reply, "Hello there.")
	}
	if thinking := streamTexts(got, "thinking"); len(thinking) != 0 {
		t.Errorf("unexpected thinking: %q", thinking)
	}
}
