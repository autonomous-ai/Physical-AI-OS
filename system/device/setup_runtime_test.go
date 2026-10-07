package device

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

func TestSetupCompletionWaitsForRuntime(t *testing.T) {
	s := &Service{config: &config.Config{SetUpCompleted: true}}
	if !s.SetupCompleted() {
		t.Fatal("normal boot must retain completion")
	}
	done := s.setupRuntime.begin()
	if s.SetupCompleted() {
		t.Fatal("configuration saved is not runtime ready")
	}
	finish, _ := s.ClaimSetupRuntime()
	duplicate, _ := s.ClaimSetupRuntime()
	if finish == nil || duplicate != nil {
		t.Fatal("must claim exactly once")
	}
	finish(nil)
	if err := s.waitSetupRuntime(context.Background(), done); err != nil {
		t.Fatal(err)
	}
	if !s.SetupCompleted() {
		t.Fatal("runtime ready must publish completion")
	}
	finish(errors.New("late error"))
	if !s.SetupCompleted() {
		t.Fatal("late result must not overwrite ready")
	}
}

func TestSetupRuntimeFailureAndRetry(t *testing.T) {
	s := &Service{config: &config.Config{SetUpCompleted: true}}
	first := s.setupRuntime.begin()
	stale, oldCtx := s.ClaimSetupRuntime()
	failure := errors.New("HAL failed")
	stale(failure)
	if err := s.waitSetupRuntime(context.Background(), first); !errors.Is(err, failure) {
		t.Fatal(err)
	}
	if s.SetupCompleted() {
		t.Fatal("failure must not report completion")
	}
	second := s.setupRuntime.begin()
	if oldCtx.Err() == nil {
		t.Fatal("previous worker must be cancelled")
	}
	stale(nil)
	if s.SetupCompleted() {
		t.Fatal("old worker must not complete new setup")
	}
	next, _ := s.ClaimSetupRuntime()
	next(nil)
	if err := s.waitSetupRuntime(context.Background(), second); err != nil {
		t.Fatal(err)
	}
}

func TestSetupRuntimeTimeoutRejectsLateSuccess(t *testing.T) {
	s := &Service{config: &config.Config{SetUpCompleted: true}}
	done := s.setupRuntime.begin()
	finish, _ := s.ClaimSetupRuntime()
	ctx, cancel := context.WithTimeout(context.Background(), time.Millisecond)
	defer cancel()
	if err := s.waitSetupRuntime(ctx, done); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
	finish(nil)
	if s.SetupCompleted() {
		t.Fatal("timed-out setup must not become successful later")
	}
	phase, msg := s.SetupRuntimeStatus()
	if phase != "failed" || msg == "" {
		t.Fatalf("missing error: %s %s", phase, msg)
	}
}

func TestSetupRuntimeConcurrentNotifications(t *testing.T) {
	s := &Service{config: &config.Config{SetUpCompleted: true}}
	done := s.setupRuntime.begin()
	var wg sync.WaitGroup
	for range 20 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if finish, _ := s.ClaimSetupRuntime(); finish != nil {
				finish(nil)
			}
			s.SetupRuntimeStatus()
		}()
	}
	wg.Wait()
	if err := s.waitSetupRuntime(context.Background(), done); err != nil {
		t.Fatal(err)
	}
}

func TestSetupRetryCancelsAndReleasesPreviousWaiter(t *testing.T) {
	s := &Service{config: &config.Config{SetUpCompleted: true}}
	first := s.setupRuntime.begin()
	finishOld, oldCtx := s.ClaimSetupRuntime()
	second := s.setupRuntime.begin()
	if oldCtx.Err() == nil {
		t.Fatal("old startup context was not cancelled")
	}
	if err := s.waitSetupRuntime(context.Background(), first); err == nil {
		t.Fatal("superseded waiter reported success")
	}
	if finishOld(nil) {
		t.Fatal("old startup completed a new run")
	}
	finish, _ := s.ClaimSetupRuntime()
	finish(nil)
	if err := s.waitSetupRuntime(context.Background(), second); err != nil {
		t.Fatal(err)
	}
}
