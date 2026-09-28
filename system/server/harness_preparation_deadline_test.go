package server

import (
	"context"
	"sync"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
)

type preparationExpirer struct {
	domain.AgentGateway
	calls []string
}

func (e *preparationExpirer) ExpireRun(_ context.Context, id, reason string) error {
	e.calls = append(e.calls, id)
	return nil
}

func TestPreparationDeadlineDoesNotRenewAndExpiresOnce(t *testing.T) {
	gateway := &preparationExpirer{}
	s := &Server{agentGateway: gateway}
	reply := &harnessReplyRequest{RunID: "run", Channel: "web"}
	now := time.Now()
	if err := s.guardHarnessPreparationWait("agent.prepare", reply, now); err != nil {
		t.Fatal(err)
	}
	if err := s.guardHarnessPreparationWait("operation.get", reply, now.Add(119*time.Second)); err != nil {
		t.Fatal(err)
	}
	if err := s.guardHarnessPreparationWait("turn.send", reply, now.Add(120*time.Second)); err == nil {
		t.Fatal("expired task admitted")
	}
	s.expireHarnessPreparationWaits(context.Background(), now.Add(120*time.Second))
	s.expireHarnessPreparationWaits(context.Background(), now.Add(121*time.Second))
	if len(gateway.calls) != 1 || gateway.calls[0] != "run" {
		t.Fatalf("expiry calls: %v", gateway.calls)
	}
	if err := s.guardHarnessPreparationWait("agent.prepare", reply, now.Add(122*time.Second)); err == nil {
		t.Fatal("same run restarted wait")
	}
	if err := s.guardHarnessPreparationWait("operation.get", &harnessReplyRequest{RunID: "resume"}, now.Add(122*time.Second)); err != nil {
		t.Fatal("new user run cannot resume", err)
	}
}

func TestPreparationDispatchAndExpiryAreSerialized(t *testing.T) {
	for i := 0; i < 50; i++ {
		gateway := &preparationExpirer{}
		s := &Server{agentGateway: gateway}
		now := time.Now()
		reply := &harnessReplyRequest{RunID: "run"}
		_ = s.guardHarnessPreparationWait("agent.prepare", reply, now)
		var wg sync.WaitGroup
		var admitted bool
		wg.Add(2)
		go func() {
			defer wg.Done()
			admitted = s.guardHarnessPreparationWait("turn.send", reply, now.Add(119*time.Second)) == nil
		}()
		go func() {
			defer wg.Done()
			s.expireHarnessPreparationWaits(context.Background(), now.Add(120*time.Second))
		}()
		wg.Wait()
		if admitted && len(gateway.calls) > 0 {
			t.Fatal("cancelled an admitted task")
		}
		if !admitted && len(gateway.calls) != 1 {
			t.Fatal("rejected dispatch without expiry")
		}
	}
}

func TestDefiniteDispatchFailureRearmsOriginalDeadline(t *testing.T) {
	gateway := &preparationExpirer{}
	s := &Server{agentGateway: gateway}
	reply := &harnessReplyRequest{RunID: "run"}
	now := time.Now()
	_ = s.guardHarnessPreparationWait("agent.prepare", reply, now)
	_ = s.guardHarnessPreparationWait("turn.send", reply, now.Add(time.Second))
	s.releaseHarnessPreparationDispatch(reply)
	s.expireHarnessPreparationWaits(context.Background(), now.Add(120*time.Second))
	if len(gateway.calls) != 1 {
		t.Fatal("failed dispatch disabled deadline")
	}
}
