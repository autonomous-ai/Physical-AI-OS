package server

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestSetupLEDWaitsBeyondSlowBootAndRetriesRejectedPaint(t *testing.T) {
	var elapsed time.Duration
	var waits []time.Duration
	healthCalls, paints := 0, 0
	retrySetupLED(context.Background(), func() bool { return false }, func(context.Context) (bool, error) {
		healthCalls++
		if healthCalls == 1 && elapsed != 0 {
			t.Fatal("first health check must be immediate")
		}
		if elapsed < 45*time.Second {
			return false, errors.New("HAL starting")
		}
		return true, nil
	}, func(context.Context) error {
		paints++
		if paints == 1 {
			return errors.New("LED temporarily unavailable")
		}
		return nil
	}, func(_ context.Context, delay time.Duration) bool {
		waits = append(waits, delay)
		elapsed += delay
		if elapsed > time.Minute {
			t.Fatal("worker did not stop after successful paint")
		}
		return true
	})
	if paints != 2 || elapsed != 55*time.Second {
		t.Fatalf("paints=%d elapsed=%v; want two attempts at 45s and 55s", paints, elapsed)
	}
	for i, delay := range waits {
		want := min(time.Second*time.Duration(1<<i), 10*time.Second)
		if delay != want {
			t.Fatalf("wait %d = %v, want %v", i, delay, want)
		}
	}
}

func TestSetupLEDStopsWhenSetupCompletes(t *testing.T) {
	for _, stage := range []string{"before health", "during health", "during backoff"} {
		t.Run(stage, func(t *testing.T) {
			completed := stage == "before health"
			healthCalls := 0
			retrySetupLED(context.Background(), func() bool { return completed }, func(context.Context) (bool, error) {
				healthCalls++
				if stage == "during health" {
					completed = true
					return true, nil
				}
				return false, nil
			}, func(context.Context) error {
				t.Fatal("must not paint after setup completes")
				return nil
			}, func(context.Context, time.Duration) bool {
				completed = true
				return true
			})
			if stage == "before health" && healthCalls != 0 || healthCalls > 1 {
				t.Fatalf("unexpected health calls: %d", healthCalls)
			}
		})
	}
}

func TestSetupLEDShutdownCancelsHealthAndBackoff(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	retrySetupLED(ctx, func() bool { return false }, func(ctx context.Context) (bool, error) {
		cancel()
		return true, ctx.Err()
	}, func(context.Context) error {
		t.Fatal("must not paint after shutdown")
		return nil
	}, func(context.Context, time.Duration) bool {
		t.Fatal("must not wait after shutdown")
		return false
	})
	if waitSetupLED(ctx, time.Hour) {
		t.Fatal("cancelled wait must exit without another attempt")
	}
}

func TestSetupLEDReadyImmediately(t *testing.T) {
	paints := 0
	retrySetupLED(context.Background(), func() bool { return false }, func(context.Context) (bool, error) {
		return true, nil
	}, func(context.Context) error {
		paints++
		return nil
	}, func(context.Context, time.Duration) bool {
		t.Fatal("ready HAL must not wait before or after successful paint")
		return false
	})
	if paints != 1 {
		t.Fatalf("paints=%d, want one", paints)
	}
}
