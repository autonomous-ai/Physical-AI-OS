package hermes

import (
	"context"
	"errors"
	"go.autonomous.ai/os/system/domain"
)

type runExpiry struct {
	ctx           context.Context
	runID, reason string
	reply         chan error
}

var _ domain.RunExpirer = (*HermesService)(nil)

// ExpireRun targets an existing native run without creating a model turn or
// representing an OS deadline as a user stop. Terminal cleanup remains owned by
// the managed reader and emits lifecycle.error after bounded remote settlement.
func (s *HermesService) ExpireRun(ctx context.Context, runID, reason string) error {
	if runID == "" || reason == "" {
		return errors.New("run expiry requires run ID and reason")
	}
	s.steeringMu.Lock()
	queue := s.runExpiries
	s.steeringMu.Unlock()
	if queue == nil {
		return errors.New("runtime has no cancellable managed run")
	}
	request := runExpiry{ctx: ctx, runID: runID, reason: reason, reply: make(chan error, 1)}
	select {
	case queue <- request:
	case <-ctx.Done():
		return ctx.Err()
	}
	select {
	case err := <-request.reply:
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
}

func expireManagedOwner(active *managedTurn, request runExpiry) error {
	if err := request.ctx.Err(); err != nil {
		return err
	}
	// A newer web request can share the original audible owner. Check both the
	// owner and latest admitted request so an old deadline cannot stop new work.
	if active == nil || active.owner != request.runID || len(active.requests) == 0 || active.requests[len(active.requests)-1].runID != request.runID {
		return errors.New("run expiry refused: requested run is no longer the active owner")
	}
	if active.expireReason != "" {
		return nil
	}
	if active.stopping || active.cancel == nil {
		return errors.New("run expiry refused: run is already stopping")
	}
	active.expireReason = request.reason
	active.stopping = true
	active.cancel()
	return nil
}
