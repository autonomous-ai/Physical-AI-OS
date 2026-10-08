package server

import (
	"testing"
	"time"
)

func TestLoginThrottleBlocksAfterMaxFailuresUntilWindowPasses(t *testing.T) {
	var lt loginThrottle
	start := time.Unix(1_700_000_000, 0)
	for i := 0; i < loginMaxFailures; i++ {
		now := start.Add(time.Duration(i) * time.Second)
		if wait := lt.retryAfter(now); wait != 0 {
			t.Fatalf("attempt %d blocked early (wait %v)", i+1, wait)
		}
		lt.recordFailure(now)
	}
	now := start.Add(loginMaxFailures * time.Second)
	wait := lt.retryAfter(now)
	if wait <= 0 || wait > loginFailureWindow {
		t.Fatalf("after %d failures wait = %v, want within (0, %v]", loginMaxFailures, wait, loginFailureWindow)
	}
	// The oldest failure ages out of the window and one attempt opens up.
	if wait := lt.retryAfter(start.Add(loginFailureWindow)); wait != 0 {
		t.Fatalf("after the window wait = %v, want 0", wait)
	}
}

func TestLoginThrottleResetsOnSuccess(t *testing.T) {
	var lt loginThrottle
	now := time.Unix(1_700_000_000, 0)
	for i := 0; i < loginMaxFailures; i++ {
		lt.recordFailure(now)
	}
	lt.reset()
	if wait := lt.retryAfter(now); wait != 0 {
		t.Fatalf("after reset wait = %v, want 0", wait)
	}
}
