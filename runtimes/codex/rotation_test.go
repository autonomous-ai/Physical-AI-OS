package codex

import "testing"

// The rotation net keys on the CONTEXT size codex reports (input + cached),
// not on the totalTokens the shared handler passes.
func TestShouldRotateSessionKeysOnContext(t *testing.T) {
	cases := []struct {
		name          string
		contextTokens int64
		want          bool
	}{
		{"idle sensing turn", 38_718, false},
		{"largest healthy sensing turn", 116_587, false},
		{"latency cliff", 134_366, true},
		{"runaway thread", 300_000, true},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := &CodexService{}
			s.lastContextTokens.Store(tc.contextTokens)
			// A large totalTokens must not matter once context is known.
			if got := s.ShouldRotateSession(999_999, 0); got != tc.want {
				t.Errorf("ShouldRotateSession(context=%d) = %v, want %v", tc.contextTokens, got, tc.want)
			}
		})
	}
}

// Before the first usage frame of the process there is no context reading, so
// the handler's totalTokens is the only number available.
func TestShouldRotateSessionFallsBackBeforeFirstUsage(t *testing.T) {
	s := &CodexService{}
	if s.ShouldRotateSession(100_000, 0) {
		t.Error("rotated below the threshold on the fallback path")
	}
	if !s.ShouldRotateSession(120_001, 0) {
		t.Error("did not rotate above the threshold on the fallback path")
	}
}

// NewSession drops the old thread's context reading — the fresh thread starts
// empty, so a turn completing without usage must not re-trip the net.
func TestNewSessionClearsContextReading(t *testing.T) {
	s := &CodexService{}
	s.lastContextTokens.Store(300_000)
	if err := s.NewSession("k"); err != nil {
		t.Fatalf("NewSession: %v", err)
	}
	if got := s.lastContextTokens.Load(); got != 0 {
		t.Errorf("lastContextTokens = %d, want 0", got)
	}
	if s.ShouldRotateSession(1_000, 0) {
		t.Error("rotated on the stale pre-rotation context size")
	}
}

// Turn count must not rotate codex on its own — the net is context-size only
// (unlike claudecode/hermes, which rotate on turns to re-anchor persona).
func TestShouldRotateSessionIgnoresTurnCount(t *testing.T) {
	s := &CodexService{}
	s.lastContextTokens.Store(1_000)
	if s.ShouldRotateSession(1_000, 500) {
		t.Error("rotated on turn count alone")
	}
}
