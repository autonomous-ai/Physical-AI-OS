package http

import "testing"

// Realtime already spoke on a [realtime-handoff] turn, so os-server must not add
// an opening filler on top of it (#564).
func TestOpeningFillerSkippedWhenRealtimeAlreadySpoke(t *testing.T) {
	cases := []struct {
		name, msg string
		skip      bool
	}{
		{"plain voice", "what time is it", false},
		{"delegated", "[voice-instruction] Momo\n[transcript] No more.", true},
		{"backstop handoff", "No more.\n[realtime-handoff] Realtime answered this aloud while you were waiting", true},
		{"vision prefix", "[vision-image]\n[voice-instruction] what is this", false},
	}
	for _, c := range cases {
		if got := realtimeAlreadySpoke(c.msg); got != c.skip {
			t.Errorf("%s: realtimeAlreadySpoke=%v, want %v", c.name, got, c.skip)
		}
	}
}
