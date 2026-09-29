package buddy

import (
	"sync"
	"testing"
	"time"
)

func TestPairingCodeBurnsAfterFiveMisses(t *testing.T) {
	p := NewPairingCodeStore(time.Minute)
	code, _ := p.Issue()
	for i := 0; i < 4; i++ {
		if p.Consume("wrong") {
			t.Fatal("wrong code accepted")
		}
	}
	if !p.Consume(code) {
		t.Fatal("correct code rejected before limit")
	}
	if p.Consume(code) {
		t.Fatal("code reused")
	}
	code, _ = p.Issue()
	var wg sync.WaitGroup
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); p.Consume("wrong") }()
	}
	wg.Wait()
	if _, ok := p.Active(); ok {
		t.Fatal("code survived five misses")
	}
	if p.Consume(code) {
		t.Fatal("burned code accepted")
	}
	code, _ = p.Issue()
	if !p.Consume(code) {
		t.Fatal("fresh code rejected")
	}
}

func TestPairingExpiredCodeRejected(t *testing.T) {
	p := NewPairingCodeStore(-time.Second)
	code, _ := p.Issue()
	if p.Consume(code) {
		t.Fatal("expired code accepted")
	}
}

func TestBurnedPairingCodeDoesNotReplaceExistingBuddy(t *testing.T) {
	svc := statusService(t)
	original, _ := statusPair(t, svc)
	code, _ := svc.IssuePairingCode()
	for i := 0; i < 5; i++ {
		if _, err := svc.ConfirmPairing("Attacker", "", "", "wrong"); err == nil {
			t.Fatal("wrong code accepted")
		}
	}
	if _, err := svc.ConfirmPairing("Replacement", "", "", code); err == nil {
		t.Fatal("burned code accepted")
	}
	if current := svc.Paired(); current == nil || current.Token != original.Token || current.BuddyID != original.BuddyID {
		t.Fatal("failed pairing replaced existing buddy")
	}
}
