// Package jev supplies optional, non-executing UI action suggestions.
package jev

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
	"time"
	"unicode/utf8"
)

const (
	Enabled = true
	// Temporary diagnostic budget for validating the proxy path before latency tuning.
	Budget   = 3 * time.Second
	cooldown = 30 * time.Second
)

type Options struct {
	Enabled  bool
	Endpoint string
	APIKey   string
}

type Tree struct {
	SnapshotID  string `json:"snapshot_id"`
	Frontmost   bool   `json:"frontmost"`
	Truncated   bool   `json:"truncated"`
	ExpiresInMS int    `json:"expires_in_ms"`
	Nodes       []Node `json:"nodes"`
}

type Node struct {
	Ref         string   `json:"ref"`
	ParentRef   string   `json:"parent_ref"`
	Role        string   `json:"role"`
	Title       string   `json:"title"`
	Description string   `json:"description"`
	Actions     []string `json:"actions"`
	Secure      bool     `json:"secure"`
	Enabled     *bool    `json:"enabled"`
}

type Suggestion struct {
	SnapshotID string `json:"snapshot_id"`
	Ref        string `json:"ref"`
	UIAction   string `json:"ui_action"`
}

type Target struct {
	Role        string `json:"role"`
	Title       string `json:"title"`
	Description string `json:"description"`
}

type Result struct {
	Target     *Target     `json:"target,omitempty"`
	Suggestion *Suggestion `json:"suggestion"`
	Reason     string      `json:"reason"`
}

// Selector is shared across requests. It never queues work or executes an action.
type Selector struct {
	client     jevClient
	busy       atomic.Bool
	retryAfter atomic.Int64
}

func New(client *http.Client) *Selector { return &Selector{client: jevClient{httpClient: client}} }

// Suggest accepts only a fresh server-observed tree. The caller must account for
// transport age with its context deadline, and retain normal authorization gates.
func (s *Selector) Suggest(ctx context.Context, goal string, tree Tree, opts Options) Result {
	started := time.Now()
	deferToAgent := func(reason string) Result { return Result{Reason: reason} }
	if s == nil || !opts.Enabled {
		return deferToAgent("disabled")
	}
	if ctx.Err() != nil {
		return deferToAgent("cancelled")
	}
	if strings.TrimSpace(opts.Endpoint) == "" || strings.TrimSpace(opts.APIKey) == "" {
		return deferToAgent("unconfigured")
	}
	if strings.TrimSpace(goal) == "" || !utf8.ValidString(goal) || utf8.RuneCountInString(goal) > 2000 {
		return deferToAgent("invalid_goal")
	}
	candidates, suggestions, ok := treeCandidates(tree)
	if !ok {
		return deferToAgent("invalid_tree")
	}
	if len(candidates) == 0 {
		return deferToAgent("no_candidates")
	}
	if time.Now().UnixNano() < s.retryAfter.Load() {
		return deferToAgent("cooldown")
	}
	if !s.busy.CompareAndSwap(false, true) {
		return deferToAgent("busy")
	}
	defer s.busy.Store(false)
	budget := Budget
	if remaining := time.Duration(tree.ExpiresInMS)*time.Millisecond - time.Since(started); remaining < budget {
		budget = remaining
	}
	deadline, cancel := context.WithTimeout(ctx, budget)
	defer cancel()
	id, err := s.client.decide(deadline, opts.Endpoint, opts.APIKey, goal, candidates)
	if err != nil || deadline.Err() != nil {
		if ctx.Err() == nil {
			s.retryAfter.Store(time.Now().Add(cooldown).UnixNano())
		}
		return deferToAgent("unavailable")
	}
	suggestion, ok := suggestions[id]
	if !ok {
		return deferToAgent("abstain")
	}
	for _, node := range tree.Nodes {
		if node.Ref == suggestion.Ref {
			return Result{Suggestion: &suggestion, Target: &Target{Role: node.Role, Title: node.Title, Description: node.Description}, Reason: "selected"}
		}
	}
	return deferToAgent("abstain")
}

func treeCandidates(tree Tree) ([]Candidate, map[string]Suggestion, bool) {
	if tree.SnapshotID == "" || len(tree.SnapshotID) > 128 || !tree.Frontmost || tree.Truncated || tree.ExpiresInMS <= 0 || tree.ExpiresInMS > 30000 || len(tree.Nodes) == 0 || len(tree.Nodes) > 500 {
		return nil, nil, false
	}
	nodes := make(map[string]Node, len(tree.Nodes))
	for _, n := range tree.Nodes {
		if n.Ref == "" || len(n.Ref) > 64 || len(n.ParentRef) > 64 || len(n.Role) > 128 || len(n.Title) > 2000 || len(n.Description) > 2000 || len(n.Actions) > 32 {
			return nil, nil, false
		}
		if _, exists := nodes[n.Ref]; exists {
			return nil, nil, false
		}
		nodes[n.Ref] = n
	}
	// Validate the entire ancestry before selecting: malformed parent links must
	// not hide a secure ancestor. Disabled ancestors also make a target unusable.
	protected := make(map[string]bool, len(nodes))
	for _, n := range tree.Nodes {
		seen := map[string]bool{}
		blocked := false
		for current := n; ; {
			if seen[current.Ref] {
				return nil, nil, false
			}
			seen[current.Ref] = true
			blocked = blocked || current.Secure || (current.Enabled != nil && !*current.Enabled)
			if current.ParentRef == "" {
				break
			}
			parent, ok := nodes[current.ParentRef]
			if !ok {
				return nil, nil, false
			}
			current = parent
		}
		protected[n.Ref] = blocked
	}
	candidates := []Candidate{}
	suggestions := map[string]Suggestion{}
	for _, n := range tree.Nodes {
		if protected[n.Ref] || n.Enabled == nil || !*n.Enabled || (strings.TrimSpace(n.Title) == "" && strings.TrimSpace(n.Description) == "") {
			continue
		}
		seenActions := map[string]bool{}
		for _, action := range n.Actions {
			if action != "press" && action != "focus" {
				continue
			}
			if seenActions[action] {
				continue
			}
			seenActions[action] = true
			if len(candidates) >= 32 {
				return nil, nil, false
			}
			id := fmt.Sprintf("ui_%d", len(candidates))
			// Only sanitized candidate metadata is sent; values, help, secure nodes and
			// the full accessibility tree never enter the provider request.
			description := fmt.Sprintf("Action %s on observed element. Role: %q; title: %q; description: %q", action, trimRunes(n.Role, 64), trimRunes(n.Title, 160), trimRunes(n.Description, 160))
			if len(description) > 2000 {
				return nil, nil, false
			}
			candidates = append(candidates, Candidate{ID: id, Description: description})
			suggestions[id] = Suggestion{SnapshotID: tree.SnapshotID, Ref: n.Ref, UIAction: action}
		}
	}
	return candidates, suggestions, true
}

func trimRunes(s string, limit int) string {
	r := []rune(s)
	if len(r) > limit {
		return string(r[:limit])
	}
	return s
}
