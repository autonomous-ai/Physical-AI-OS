package jev

import (
	"context"
	"errors"
	"testing"
	"time"
)

type fakeJevDecider func(context.Context, string, string, []Candidate) (Selection, error)

func (f fakeJevDecider) decide(ctx context.Context, endpoint, key, text string, candidates []Candidate) (Selection, error) {
	return f(ctx, key, text, candidates)
}

func TestJevDeadlineAndErrorsFallThroughWithoutLateExecution(t *testing.T) {
	for _, late := range []bool{false, true} {
		providerCalls := 0
		r := &Resolver{client: fakeJevDecider(func(ctx context.Context, _ string, _ string, _ []Candidate) (Selection, error) {
			providerCalls++
			if late {
				<-ctx.Done()
				return Selection{Intent: "dim"}, nil // Even a late success cannot execute hardware.
			}
			return Selection{}, errors.New("provider unavailable")
		})}
		opts := Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true, APIKey: "test", Timeout: 5 * time.Millisecond}
		start := time.Now()
		if r.Resolve(context.Background(), "ánh sáng chói quá", Candidates(), opts).Intent != "" {
			t.Fatal("failed or late decision executed")
		}
		if time.Since(start) > time.Second {
			t.Fatal("decision budget was ignored")
		}
		if r.Resolve(context.Background(), "ánh sáng chói quá", Candidates(), opts).Intent != "" || providerCalls != 1 {
			t.Fatal("error cooldown retried provider")
		}

	}
}

func TestJevConcurrentMissSkipsInsteadOfWaiting(t *testing.T) {
	entered, release := make(chan struct{}), make(chan struct{})
	r := &Resolver{client: fakeJevDecider(func(context.Context, string, string, []Candidate) (Selection, error) {
		close(entered)
		<-release
		return Selection{}, nil
	})}
	opts := Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true, APIKey: "test"}
	done := make(chan struct{})
	go func() {
		defer close(done)
		r.Resolve(context.Background(), "ánh sáng chói quá", Candidates(), opts)
	}()
	<-entered
	got := r.Resolve(context.Background(), "ánh sáng chói quá", Candidates(), opts)
	close(release)
	<-done
	if got.Intent != "" {
		t.Fatal("busy request should fall through")
	}
}

func TestJevBoundsAndCancelledInputNeverContactProvider(t *testing.T) {
	r := &Resolver{client: fakeJevDecider(func(context.Context, string, string, []Candidate) (Selection, error) {
		t.Fatal("invalid input reached provider")
		return Selection{}, nil
	})}
	opts := Options{Endpoint: "https://proxy.example.test/jev/decisions", Enabled: true, APIKey: "test"}
	for _, text := range []string{"", string(make([]byte, jevMaxInputBytes+1))} {
		if r.Resolve(context.Background(), text, Candidates(), opts).Intent != "" {
			t.Fatal("unexpected result")
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if r.Resolve(ctx, "ánh sáng chói quá", Candidates(), opts).Intent != "" {
		t.Fatal("cancelled input executed")
	}

}

func TestJevPreservesInstructionNegationAndDoesNotMixTranscript(t *testing.T) {
	text := "[voice-instruction] Don't change my light; check my email.\n[transcript] light off [snapshot: /private/frame.jpg]"
	if got := jevText(text); got != "Don't change my light; check my email." {
		t.Fatalf("got %q", got)
	}
	for _, utterance := range []string{"[đừng] giảm sáng", "[when I get home] giảm sáng", "giảm sáng (but only tomorrow)"} {
		wrapped := "[user] [ambient] Unknown Speaker: [voice:test] " + utterance + " (audio saved at /private/test.wav)"
		if got := jevText(wrapped); got != utterance {
			t.Fatalf("lost qualifier: %q -> %q", utterance, got)
		}
	}
}

func TestJevResolverBudget(t *testing.T) {
	for _, tc := range []struct {
		name            string
		requested, want time.Duration
	}{
		{"default", 0, 3 * time.Second},
		{"negative", -time.Second, 3 * time.Second},
		{"three seconds", 3 * time.Second, 3 * time.Second},
		{"bounded", 10 * time.Second, 3 * time.Second},
		{"explicit shorter", time.Second, time.Second},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := &Resolver{client: fakeJevDecider(func(ctx context.Context, _, _ string, _ []Candidate) (Selection, error) {
				deadline, ok := ctx.Deadline()
				remaining := time.Until(deadline)
				if !ok || remaining > tc.want || remaining < tc.want-250*time.Millisecond {
					t.Fatalf("unexpected remaining budget: %v, want near %v", remaining, tc.want)
				}
				return Selection{}, nil
			})}
			r.Resolve(context.Background(), "Tắt đèn", Candidates(), Options{Enabled: true, Endpoint: testJevEndpoint, APIKey: "test", Timeout: tc.requested})
		})
	}
}

func TestJevResolverRejectsInvalidSelectedArguments(t *testing.T) {
	candidates := []Candidate{{ID: "track", Description: "Track a target", Parameters: map[string]Parameter{"target": {Description: "Requested target", Options: []string{"cell phone"}}}}}
	for _, selection := range []Selection{
		{Intent: "track"},
		{Intent: "track", Parameters: map[string]string{"target": "person"}},
		{Intent: "track", Parameters: map[string]string{"target": "cell phone; run shell"}},
		{Intent: "track", Parameters: map[string]string{"target": "cell phone", "extra": "value"}},
		{Intent: "unknown", Parameters: map[string]string{"target": "cell phone"}},
	} {
		r := &Resolver{client: fakeJevDecider(func(context.Context, string, string, []Candidate) (Selection, error) { return selection, nil })}
		got := r.Resolve(context.Background(), "Track my phone", candidates, Options{Enabled: true, Endpoint: testJevEndpoint, APIKey: "test"})
		if got.Intent != "" {
			t.Fatalf("accepted invalid selection %+v", got)
		}
	}
}

// The pre-filter may only skip Jev for text no catalog intent could accept.
func TestMayBeDeviceCommandKeepsLiveCorpus(t *testing.T) {
	for _, tc := range liveIntentCases() {
		if tc.want != "" && !MayBeDeviceCommand(tc.text) {
			t.Errorf("positive example %q filtered out before Jev", tc.text)
		}
	}
	for _, text := range []string{"Let me turn on the light.", "too bright", "I don't want to listen to music. Stop it.", "Mấy giờ rồi"} {
		if !MayBeDeviceCommand(text) {
			t.Errorf("%q filtered out before Jev", text)
		}
	}
	calls := 0
	r := &Resolver{client: fakeJevDecider(func(context.Context, string, string, []Candidate) (Selection, error) {
		calls++
		return Selection{}, nil
	})}
	opts := Options{Enabled: true, Endpoint: testJevEndpoint, APIKey: "test"}
	for _, text := range []string{"What is two plus two?", "Unknown Speaker: [voice:v1] How are you today? (audio saved at /tmp/x.wav)", "Send text to my Notion"} {
		r.Resolve(context.Background(), text, Candidates(), opts)
	}
	if calls != 0 {
		t.Fatalf("Jev called %d times for text without device vocabulary", calls)
	}
	r.Resolve(context.Background(), "Please switch this light on.", Candidates(), opts)
	if calls != 1 {
		t.Fatal("device command did not reach Jev")
	}
}
