package hermes

import (
	"strings"
	"testing"
)

func TestShouldRotateSession(t *testing.T) {
	s := &HermesService{}
	cases := []struct {
		name   string
		tokens int
		turns  int
		want   bool
	}{
		{"fresh: below both thresholds", 20_000, 5, false},
		{"turn count trips primary trigger", 20_000, rotateMaxTurns, true},
		{"one turn over count", 20_000, rotateMaxTurns + 1, true},
		{"token spike trips secondary trigger", rotateTokenThreshold, 1, true},
		{"just under both", rotateTokenThreshold - 1, rotateMaxTurns - 1, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := s.ShouldRotateSession(tc.tokens, tc.turns); got != tc.want {
				t.Errorf("ShouldRotateSession(%d,%d) = %v, want %v", tc.tokens, tc.turns, got, tc.want)
			}
		})
	}
}

func TestConversationRotation(t *testing.T) {
	s := &HermesService{}

	n1 := s.conversationName()
	if !strings.HasPrefix(n1, Conversation+"-") {
		t.Fatalf("seed name %q should start with %q-", n1, Conversation)
	}
	if n1 == Conversation {
		t.Fatalf("seed must not reuse the bare base name (would re-attach to a bloated chain)")
	}
	if s.conversationName() != n1 {
		t.Errorf("conversationName must be stable until rotation")
	}

	s.lastResponseID.Store("resp_old")
	s.rotateConversation()
	n2 := s.conversationName()
	if n2 == n1 {
		t.Errorf("rotation must change the conversation name (was %q)", n1)
	}
	if !strings.HasPrefix(n2, Conversation+"-") {
		t.Errorf("rotated name %q malformed", n2)
	}
	if got, _ := s.lastResponseID.Load().(string); got != "" {
		t.Errorf("rotation must clear lastResponseID, got %q", got)
	}
}
