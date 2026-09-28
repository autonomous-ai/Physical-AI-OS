package jev

import (
	"context"
	"log/slog"
	"strings"
	"sync/atomic"
	"time"
)

const (
	// Match the Hermes Jev plugin diagnostic budget before latency tuning.
	DefaultTimeout   = 3 * time.Second
	MaxTimeout       = 3 * time.Second
	jevErrorCooldown = 30 * time.Second
	jevMaxInputBytes = 2000
)

// Options are explicit per-request settings. Disabled is the zero value.
// Endpoint and APIKey come from config.json's Autonomous proxy settings.
type Options struct {
	Enabled  bool
	Endpoint string
	APIKey   string
	Timeout  time.Duration
}

type jevDecider interface {
	decide(context.Context, string, string, string, []Candidate) (Selection, error)
}

// Resolver classifies a request without performing any hardware action.
// Reuse one per handler: concurrent calls skip rather than queue, and provider
// errors open a brief cooldown. No hardware work runs in a detached goroutine.
type Resolver struct {
	client     jevDecider
	harness    bool
	busy       atomic.Bool
	retryAfter atomic.Int64
}

func NewResolver() *Resolver { return &Resolver{client: &jevClient{}} }

// Resolve returns a code-owned selection or an empty Intent to defer to the
// main agent. The caller owns capability checks and all hardware execution.
func (r *Resolver) Resolve(ctx context.Context, text string, candidates []Candidate, options Options) Selection {
	skip := func(reason string) Selection {
		// Routing diagnostics must not expose the utterance or provider settings.
		slog.Info("intent Jev decision", "component", "intent", "outcome", "skipped", "reason", reason)
		return Selection{}
	}
	if ctx.Err() != nil {
		return skip("cancelled")
	}
	if r == nil {
		return skip("unavailable")
	}
	if !options.Enabled {
		return skip("disabled")
	}
	if strings.TrimSpace(options.Endpoint) == "" || strings.TrimSpace(options.APIKey) == "" {
		return skip("missing_config")
	}
	maxInputBytes := jevMaxInputBytes
	if r.harness {
		// Project names, paths and structured task context must retain their case
		// and punctuation; hardware speech normalization is not suitable here.
		text = strings.TrimSpace(text)
		maxInputBytes = 8000
	} else {
		text = jevText(text)
	}
	if text == "" || len(text) > maxInputBytes {
		return skip("invalid_input")
	}
	if len(candidates) == 0 {
		return skip("no_candidates")
	}
	if time.Now().UnixNano() < r.retryAfter.Load() {
		return skip("cooldown")
	}
	if !r.busy.CompareAndSwap(false, true) {
		return skip("busy")
	}
	defer r.busy.Store(false)
	budget := options.Timeout
	if budget <= 0 {
		budget = DefaultTimeout
	} else if budget > MaxTimeout {
		budget = MaxTimeout
	}
	deadline, cancel := context.WithTimeout(ctx, budget)
	defer cancel()
	started := time.Now()
	id, err := r.client.decide(deadline, options.Endpoint, options.APIKey, text, candidates)
	elapsed := time.Since(started).Milliseconds()
	outcome := "abstain"
	defer func() {
		// Never log credentials, provider bodies or the utterance here.
		attrs := []any{"component", "intent", "outcome", outcome, "decision_ms", elapsed}
		if outcome == "selected" {
			attrs = append(attrs, "intent", id.Intent)
			if len(id.Parameters) > 0 {
				attrs = append(attrs, "parameters", id.Parameters)
			}
		}
		slog.Info("intent Jev decision", attrs...)
	}()
	if err != nil || deadline.Err() != nil {
		outcome = "error"
		if ctx.Err() == nil {
			r.retryAfter.Store(time.Now().Add(jevErrorCooldown).UnixNano())
		}
		return Selection{}
	}
	for _, candidate := range candidates {
		if validJevSelection(id, candidate) {
			outcome = "selected"
			return id
		}
	}
	return Selection{}
}
