package versioncache

import (
	"context"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"
)

// All caches share one admission queue: even an early status poll must not
// launch a CLI alongside critical hardware startup or another version probe.
var startup = func() *atomic.Pointer[startupAdmission] {
	p := new(atomic.Pointer[startupAdmission])
	p.Store(newStartupAdmission(context.Background(), nil, 0))
	return p
}()

type startupAdmission struct {
	ctx     context.Context
	ready   func(context.Context) bool
	maxWait time.Duration
	once    sync.Once
	opened  chan struct{}
	slot    chan struct{}
}

func newStartupAdmission(ctx context.Context, ready func(context.Context) bool, maxWait time.Duration) *startupAdmission {
	return &startupAdmission{ctx: ctx, ready: ready, maxWait: maxWait,
		opened: make(chan struct{}), slot: make(chan struct{}, 1)}
}

// ConfigureStartup defers CLI probes until ready is true or maxWait expires, then serializes them.
// Call once before starting services; a nil ready opens immediately.
func ConfigureStartup(ctx context.Context, ready func(context.Context) bool, maxWait time.Duration) {
	startup.Store(newStartupAdmission(ctx, ready, maxWait))
}

func (a *startupAdmission) open() {
	defer close(a.opened)
	if a.ready == nil || a.maxWait <= 0 {
		return
	}
	ctx, cancel := context.WithTimeout(a.ctx, a.maxWait)
	defer cancel()
	defer func() {
		if ctx.Err() == context.DeadlineExceeded && a.ctx.Err() == nil {
			slog.Info("runtime version probes admitted after startup deadline", "component", "versioncache")
		}
	}()
	for ctx.Err() == nil {
		if a.ready(ctx) {
			slog.Info("runtime version probes admitted after hardware readiness", "component", "versioncache")
			return
		}
		timer := time.NewTimer(time.Second)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}

func (a *startupAdmission) acquire() bool {
	a.once.Do(func() { go a.open() })
	select {
	case <-a.ctx.Done():
		return false
	case <-a.opened:
	}
	select {
	case <-a.ctx.Done():
		return false
	case a.slot <- struct{}{}:
		if a.ctx.Err() != nil {
			a.release()
			return false
		}
		return true
	}
}

func (a *startupAdmission) release() { <-a.slot }
