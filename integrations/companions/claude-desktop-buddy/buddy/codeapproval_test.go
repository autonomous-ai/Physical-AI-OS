package buddy

import (
	"context"
	"testing"
	"time"

	"claude-desktop-buddy/httpapi"
)

// testBridge points at an unreachable host so HAL posts fail fast.
func testBridge() *Bridge { return NewBridge("http://127.0.0.1:1", "http://127.0.0.1:1") }

func TestCodeApprovals_AllowUnblocksRequest(t *testing.T) {
	c := NewCodeApprovals(testBridge(), 2*time.Second)
	got := make(chan string, 1)
	go func() {
		d, _ := c.Request(context.Background(), httpapi.CodeApprovalRequest{ID: "a", Tool: "Bash"})
		got <- d
	}()
	time.Sleep(100 * time.Millisecond)
	if err := c.Approve("a"); err != nil {
		t.Fatalf("approve: %v", err)
	}
	select {
	case d := <-got:
		if d != "allow" {
			t.Fatalf("decision=%q want allow", d)
		}
	case <-time.After(time.Second):
		t.Fatal("Request did not return after Approve")
	}
}

func TestCodeApprovals_DenyUnblocksRequest(t *testing.T) {
	c := NewCodeApprovals(testBridge(), 2*time.Second)
	got := make(chan string, 1)
	go func() {
		d, _ := c.Request(context.Background(), httpapi.CodeApprovalRequest{ID: "b", Tool: "Bash"})
		got <- d
	}()
	time.Sleep(100 * time.Millisecond)
	if err := c.Deny("b"); err != nil {
		t.Fatalf("deny: %v", err)
	}
	if d := <-got; d != "deny" {
		t.Fatalf("decision=%q want deny", d)
	}
}

func TestCodeApprovals_Timeout(t *testing.T) {
	c := NewCodeApprovals(testBridge(), 150*time.Millisecond)
	d, _ := c.Request(context.Background(), httpapi.CodeApprovalRequest{ID: "c", Tool: "Bash"})
	if d != "timeout" {
		t.Fatalf("decision=%q want timeout", d)
	}
}

func TestCodeApprovals_ContextCancelDefers(t *testing.T) {
	c := NewCodeApprovals(testBridge(), 5*time.Second)
	ctx, cancel := context.WithCancel(context.Background())
	got := make(chan string, 1)
	go func() {
		d, _ := c.Request(ctx, httpapi.CodeApprovalRequest{ID: "d", Tool: "Bash"})
		got <- d
	}()
	time.Sleep(100 * time.Millisecond)
	cancel()
	if d := <-got; d != "timeout" {
		t.Fatalf("decision=%q want timeout on cancel", d)
	}
}

func TestCodeApprovals_UnknownIDNoPending(t *testing.T) {
	c := NewCodeApprovals(testBridge(), time.Second)
	if err := c.Approve("missing"); err != httpapi.ErrNoPending {
		t.Fatalf("err=%v want ErrNoPending", err)
	}
}

func TestCodeApprovals_DoubleResolveIgnored(t *testing.T) {
	c := NewCodeApprovals(testBridge(), 2*time.Second)
	go c.Request(context.Background(), httpapi.CodeApprovalRequest{ID: "e", Tool: "Bash"})
	time.Sleep(100 * time.Millisecond)
	if err := c.Approve("e"); err != nil {
		t.Fatalf("first approve: %v", err)
	}
	// Second resolve races the Request cleanup; it must never block or panic.
	_ = c.Deny("e")
}

func TestCodeApprovals_Pending(t *testing.T) {
	c := NewCodeApprovals(testBridge(), 2*time.Second)
	go c.Request(context.Background(), httpapi.CodeApprovalRequest{ID: "f", Tool: "Bash", Hint: "ls"})
	time.Sleep(100 * time.Millisecond)
	p := c.Pending()
	if len(p) != 1 || p[0].ID != "f" {
		t.Fatalf("pending=%+v want one id=f", p)
	}
}
