package device

import (
	"context"
	"fmt"
	"sync"
	"time"
)

// setupRuntime tracks only an explicit onboarding run, never a normal boot.
// The persisted setup flag still starts reconciliation; public completion waits
// for that reconciliation to bring HAL and voice up.
type setupRuntime struct {
	mu      sync.Mutex
	phase   string
	err     string
	done    chan error
	claimed bool
	ctx     context.Context
	cancel  context.CancelFunc
}

func (r *setupRuntime) begin() chan error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.cancel != nil {
		if r.phase == "preparing" {
			r.done <- fmt.Errorf("setup superseded by a new attempt")
		}
		r.cancel()
	}
	r.ctx, r.cancel = context.WithCancel(context.Background())
	r.phase, r.err, r.claimed = "preparing", "", false
	r.done = make(chan error, 1)
	return r.done
}

func (s *Service) SetupRuntimeStatus() (string, string) {
	s.setupRuntime.mu.Lock()
	defer s.setupRuntime.mu.Unlock()
	return s.setupRuntime.phase, s.setupRuntime.err
}

func (s *Service) SetupCompleted() bool {
	phase, _ := s.SetupRuntimeStatus()
	return s.config.SetUpCompleted && phase != "preparing" && phase != "failed"
}

// ClaimSetupRuntime binds the startup worker to this run. Other config
// notifications cannot start a second worker for the same onboarding run.
func (s *Service) ClaimSetupRuntime() (func(error) bool, context.Context) {
	r := &s.setupRuntime
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.phase != "preparing" || r.claimed {
		return nil, nil
	}
	r.claimed = true
	done := r.done
	return func(err error) bool { return s.finishSetupRuntime(done, err) }, r.ctx
}

func (s *Service) finishSetupRuntime(done chan error, err error) bool {
	r := &s.setupRuntime
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.done != done || r.phase != "preparing" {
		return false
	}
	if err != nil {
		r.phase, r.err = "failed", err.Error()
		r.cancel()
	} else {
		r.phase = "ready"
	}
	done <- err
	return true
}

func (s *Service) waitSetupRuntime(ctx context.Context, done chan error) error {
	select {
	case err := <-done:
		return err
	case <-ctx.Done():
		err := fmt.Errorf("preparing device timed out: %w", ctx.Err())
		if !s.finishSetupRuntime(done, err) {
			return <-done
		}
		return err
	}
}

const setupRuntimeTimeout = 5 * time.Minute
