package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"go.autonomous.ai/os/system/lib/hal"
)

// waitHALMain is an optional ExecStartPre that lets HAL boot first; a failed HAL
// must not block text-only agent access indefinitely.
func waitHALMain() int {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	started := time.Now()
	ready, err := waitForHAL(ctx, time.Minute, time.Second, func(ctx context.Context) error {
		_, err := hal.GetHealthContext(ctx)
		return err
	})
	if err != nil {
		log.Printf("[agent-startup] hardware wait canceled: %v", err)
		return 1
	}
	log.Printf("[agent-startup] hardware wait finished ready=%t elapsed=%s", ready, time.Since(started).Round(time.Millisecond))
	return 0
}

func waitForHAL(parent context.Context, timeout, interval time.Duration, check func(context.Context) error) (bool, error) {
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	for ctx.Err() == nil {
		requestCtx, cancelRequest := context.WithTimeout(ctx, time.Second)
		err := check(requestCtx)
		cancelRequest()
		if err == nil && ctx.Err() == nil {
			return true, nil
		}
		timer := time.NewTimer(interval)
		select {
		case <-ctx.Done():
			timer.Stop()
		case <-timer.C:
		}
	}
	return false, parent.Err()
}
