package main

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestHardwareWaitReadyAndRetries(t *testing.T) {
	calls := 0
	ready, err := waitForHAL(context.Background(), time.Second, time.Millisecond, func(context.Context) error {
		calls++
		if calls < 3 {
			return errors.New("starting")
		}
		return nil
	})
	if !ready || err != nil || calls != 3 {
		t.Fatalf("ready=%t err=%v calls=%d", ready, err, calls)
	}
}

func TestHardwareWaitFailsOpenOnDeadline(t *testing.T) {
	ready, err := waitForHAL(context.Background(), 20*time.Millisecond, time.Second, func(ctx context.Context) error {
		<-ctx.Done()
		return ctx.Err()
	})
	if ready || err != nil {
		t.Fatalf("HAL timeout must allow agent startup: ready=%t err=%v", ready, err)
	}
}

func TestHardwareWaitCancelsInFlightRequest(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	ready, err := waitForHAL(ctx, time.Minute, time.Second, func(ctx context.Context) error {
		cancel()
		<-ctx.Done()
		return ctx.Err()
	})
	if ready || !errors.Is(err, context.Canceled) {
		t.Fatalf("ready=%t err=%v", ready, err)
	}
}
