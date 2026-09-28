package server

import (
	"errors"
	"testing"

	"go.autonomous.ai/os/system/environment"
)

func TestWakeGreetingWithoutEnvironmentDoesNotWait(t *testing.T) {
	for _, allowed := range []bool{true, false} {
		startup := environment.NewStartupCoordinator()
		calls := 0
		err := sendWakeGreetingWithEnvironment("wake now", startup, allowed, func(prompt string) (string, error) {
			calls++
			if prompt != "wake now" {
				t.Fatalf("cold greeting changed: %q", prompt)
			}
			return "run-1", nil
		})
		if err != nil || calls != 1 {
			t.Fatalf("greeting calls=%d error=%v", calls, err)
		}
	}
}

func TestWakeGreetingPropagatesGatewayFailure(t *testing.T) {
	want := errors.New("gateway unavailable")
	err := sendWakeGreetingWithEnvironment("wake", environment.NewStartupCoordinator(), true, func(string) (string, error) {
		return "", want
	})
	if !errors.Is(err, want) {
		t.Fatalf("got %v, want %v", err, want)
	}
}

func TestStartupGreetingRespectsHALSleep(t *testing.T) {
	for _, tc := range []struct {
		name                        string
		ready, expression, sleeping bool
		err                         error
		want, probe                 bool
	}{
		{"awake", true, true, false, nil, true, true},
		{"restart asleep", true, true, true, nil, false, true},
		{"HAL unavailable", true, true, false, errors.New("offline"), false, true},
		{"gateway unavailable", false, true, false, nil, false, false},
		{"no expression", true, false, false, nil, true, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			called := false
			got := startupGreetingAllowed(tc.ready, tc.expression, func() (bool, error) {
				called = true
				return tc.sleeping, tc.err
			})
			if got != tc.want || called != tc.probe {
				t.Fatalf("allowed=%v probe=%v; want %v %v", got, called, tc.want, tc.probe)
			}
		})
	}
}
