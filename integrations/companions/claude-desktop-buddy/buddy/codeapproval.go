package buddy

import (
	"context"
	"log"
	"sync"
	"time"

	"claude-desktop-buddy/httpapi"
)

// CodeApprovals implements httpapi.CodeApprovalService: Request blocks per id until Approve/Deny or the ttl expires.
type CodeApprovals struct {
	mu     sync.Mutex
	items  map[string]*codeEntry
	bridge *Bridge
	ttl    time.Duration
}

type codeEntry struct {
	req     httpapi.CodeApprovalRequest
	ch      chan string // buffered(1); first writer wins, later resolves are no-ops
	created time.Time
}

// NewCodeApprovals builds the service; ttl bounds how long Request blocks before returning "timeout".
func NewCodeApprovals(bridge *Bridge, ttl time.Duration) *CodeApprovals {
	if ttl <= 0 {
		ttl = 55 * time.Second
	}
	return &CodeApprovals{
		items:  make(map[string]*codeEntry),
		bridge: bridge,
		ttl:    ttl,
	}
}

func (c *CodeApprovals) Request(ctx context.Context, req httpapi.CodeApprovalRequest) (string, error) {
	if req.ID == "" {
		return "timeout", nil
	}
	e := &codeEntry{req: req, ch: make(chan string, 1), created: time.Now()}

	c.mu.Lock()
	c.items[req.ID] = e
	c.mu.Unlock()
	defer func() {
		c.mu.Lock()
		delete(c.items, req.ID)
		c.mu.Unlock()
	}()

	log.Printf("[code-approval] pending %s tool=%s hint=%q", req.ID, req.Tool, req.Hint)
	// Run blocking HAL calls off the request goroutine so a slow device can't eat the answer window.
	go c.bridge.announceCodeApproval(req)
	defer func() { go c.bridge.restoreAfterCodeApproval() }()

	select {
	case d := <-e.ch:
		log.Printf("[code-approval] %s resolved: %s", req.ID, d)
		return d, nil
	case <-time.After(c.ttl):
		log.Printf("[code-approval] %s timed out after %s", req.ID, c.ttl)
		return "timeout", nil
	case <-ctx.Done():
		// Hook gave up / client disconnected — treat as no decision.
		log.Printf("[code-approval] %s cancelled: %v", req.ID, ctx.Err())
		return "timeout", ctx.Err()
	}
}

func (c *CodeApprovals) Approve(id string) error { return c.resolve(id, "allow") }
func (c *CodeApprovals) Deny(id string) error    { return c.resolve(id, "deny") }

func (c *CodeApprovals) resolve(id, decision string) error {
	c.mu.Lock()
	e := c.items[id]
	c.mu.Unlock()
	if e == nil {
		return httpapi.ErrNoPending
	}
	// Buffered(1) non-blocking send: first decision wins, later resolves are no-ops.
	select {
	case e.ch <- decision:
	default:
	}
	return nil
}

func (c *CodeApprovals) Pending() []httpapi.CodeApprovalRequest {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]httpapi.CodeApprovalRequest, 0, len(c.items))
	for _, e := range c.items {
		out = append(out, e.req)
	}
	return out
}
