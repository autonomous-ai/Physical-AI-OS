package server

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/flow"
)

const harnessPreparationWaitLimit = 120 * time.Second
const harnessPreparationWaitReason = domain.RunExpiryErrorPrefix + "Harness preparation wait timed out; the task was not sent. Preparation may continue remotely. Resume the saved intent on a new user turn after resolving Harness readiness."

type harnessPreparationWait struct {
	deadline   time.Time
	expired    bool
	dispatched bool
}

// The lifetime belongs to the server, not an individual polling HTTP request.
func (s *Server) watchHarnessPreparationWaits(ctx context.Context) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			s.expireHarnessPreparationWaits(ctx, now)
		}
	}
}

// Serialize deadline expiry with dispatch admission: once admitted, delivery
// may be uncertain and this preparation watchdog must never cancel its owner.
func (s *Server) guardHarnessPreparationWait(kind string, reply *harnessReplyRequest, now time.Time) error {
	if reply == nil {
		return nil
	}
	s.harnessPreparationMu.Lock()
	defer s.harnessPreparationMu.Unlock()
	wait := s.harnessPreparationWaits[reply.RunID]
	if wait == nil {
		if kind != "agent.prepare" && kind != "operation.get" {
			return nil
		}
		if s.harnessPreparationWaits == nil {
			s.harnessPreparationWaits = make(map[string]*harnessPreparationWait)
		}
		// Retain expired routes long enough to reject late callbacks without an
		// unbounded cache. The helper's persisted budget survives OS restarts.
		for id, old := range s.harnessPreparationWaits {
			if now.Sub(old.deadline) > 24*time.Hour {
				delete(s.harnessPreparationWaits, id)
			}
		}
		if len(s.harnessPreparationWaits) >= 4096 {
			return fmt.Errorf("Harness preparation wait registry full; end this turn and retain the intent")
		}
		wait = &harnessPreparationWait{deadline: now.Add(harnessPreparationWaitLimit)}
		s.harnessPreparationWaits[reply.RunID] = wait
	}
	if wait.dispatched {
		return nil
	}
	if wait.expired || !now.Before(wait.deadline) {
		return fmt.Errorf("PREPARATION_WAIT_EXPIRED: %s", harnessPreparationWaitReason)
	}
	if kind == "turn.send" || kind == "question.answer" {
		wait.dispatched = true
	}
	return nil
}

func (s *Server) expireHarnessPreparationWaits(ctx context.Context, now time.Time) {
	var expired []string
	s.harnessPreparationMu.Lock()
	for id, wait := range s.harnessPreparationWaits {
		if !wait.expired && !wait.dispatched && !now.Before(wait.deadline) {
			wait.expired = true
			expired = append(expired, id)
		}
	}
	s.harnessPreparationMu.Unlock()
	for _, id := range expired {
		flow.Log("harness_preparation_wait_expired", map[string]any{"run_id": id, "task_dispatched": false, "reason": harnessPreparationWaitReason}, id)
		expirer, ok := s.agentGateway.(domain.RunExpirer)
		if !ok {
			slog.Warn("runtime cannot expire a scoped preparation wait", "run_id", id)
			continue
		}
		// Expiry does not send a new prompt or a global /stop. The runtime must
		// match this exact owner and emit its normal terminal error lifecycle.
		stopCtx, cancel := context.WithTimeout(ctx, 35*time.Second)
		err := expirer.ExpireRun(stopCtx, id, harnessPreparationWaitReason)
		cancel()
		if err != nil {
			slog.Warn("preparation wait expiry", "run_id", id, "error", err)
		}
	}
}

// A definite pre-delivery failure must not disable the wait budget. Unknown
// delivery remains admitted and is reconciled by the task receipt workflow.
func (s *Server) releaseHarnessPreparationDispatch(reply *harnessReplyRequest) {
	if reply == nil {
		return
	}
	s.harnessPreparationMu.Lock()
	defer s.harnessPreparationMu.Unlock()
	if wait := s.harnessPreparationWaits[reply.RunID]; wait != nil {
		wait.dispatched = false
	}
}
