// Package telemetry queues and sends product analytics events.
// Report never blocks or fails its caller; every event is logged locally and losses are counted.
package telemetry

import (
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/system/lib/analytics"
)

// Event is one telemetry observation. Params carry the tracker's own fields;
// common fields (version, runtime, counters) are added by this package.
type Event struct {
	// Name is the analytics event_name, e.g. "voice_metrics_interaction".
	Name string
	// ID de-duplicates retries; producers that can retry must set it.
	ID string
	// Params are the tracker's fields. Never include transcripts, reply text, audio or credentials.
	Params map[string]any
}

const (
	queueSize   = 256
	sendTimeout = 10 * time.Second
	// dedupeTTL bounds the seen-ID registry (well past any retry window).
	dedupeTTL = 30 * time.Minute
	logPrefix = "[telemetry]"
)

// Enabled reports whether an analytics endpoint is configured; events are always logged locally.
func Enabled() bool { return analytics.Endpoint() != "" }

// sendFunc is the transport signature (analytics.TrackEvent in production).
type sendFunc func(ctx context.Context, name string, params map[string]any) error

type reporter struct {
	queue chan Event
	// Per reporter so a swapped test transport can't be reached by an old worker.
	sender sendFunc

	startOnce sync.Once

	commonMu sync.RWMutex
	common   map[string]any

	seenMu sync.Mutex
	seen   map[string]time.Time

	dropped atomic.Int64 // queue full
	failed  atomic.Int64 // transport error
	deduped atomic.Int64
}

var global = newReporter(analytics.TrackEvent)

func newReporter(send sendFunc) *reporter {
	return &reporter{
		queue:  make(chan Event, queueSize),
		sender: send,
		common: map[string]any{},
		seen:   map[string]time.Time{},
	}
}

// SetCommon replaces the fields stamped on every event; call once at startup.
func SetCommon(fields map[string]any) {
	global.commonMu.Lock()
	global.common = fields
	global.commonMu.Unlock()
}

// Report queues ev without blocking; a full queue drops and counts it. Safe before Start.
func Report(ev Event) {
	if ev.Name == "" {
		return
	}
	global.startOnce.Do(func() { go global.run(context.Background()) })

	if global.isDuplicate(ev.ID) {
		global.deduped.Add(1)
		slog.Info(logPrefix+" duplicate event ignored", "component", "telemetry",
			"event_name", ev.Name, "event_id", ev.ID)
		return
	}

	// Log before queueing: this is the device-local record whether or not it is sent.
	slog.Info(logPrefix+" event", "component", "telemetry",
		"event_name", ev.Name, "event_id", ev.ID, "params", compactJSON(ev.Params))

	if !Enabled() {
		slog.Debug(logPrefix+" not sent -- no analytics endpoint configured", "component", "telemetry",
			"event_name", ev.Name, "event_id", ev.ID)
		return
	}

	select {
	case global.queue <- ev:
	default:
		n := global.dropped.Add(1)
		slog.Warn(logPrefix+" event dropped -- queue full", "component", "telemetry",
			"event_name", ev.Name, "event_id", ev.ID, "dropped_total", n)
	}
}

// Stats returns dropped, failed and deduplicated event counts.
func Stats() (dropped, failed, deduped int64) {
	return global.dropped.Load(), global.failed.Load(), global.deduped.Load()
}

func (r *reporter) isDuplicate(id string) bool {
	if id == "" {
		return false
	}
	now := time.Now()
	r.seenMu.Lock()
	defer r.seenMu.Unlock()
	if _, ok := r.seen[id]; ok {
		return true
	}
	cutoff := now.Add(-dedupeTTL)
	for k, ts := range r.seen {
		if ts.Before(cutoff) {
			delete(r.seen, k)
		}
	}
	r.seen[id] = now
	return false
}

func (r *reporter) run(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case ev := <-r.queue:
			r.send(ctx, ev)
		}
	}
}

func (r *reporter) send(ctx context.Context, ev Event) {
	params := make(map[string]any, len(ev.Params)+8)
	r.commonMu.RLock()
	for k, v := range r.common {
		params[k] = v
	}
	r.commonMu.RUnlock()
	for k, v := range ev.Params {
		params[k] = v
	}
	params["event_id"] = ev.ID
	// Loss counters ride along so warehouse success rates can account for missing data.
	params["telemetry_dropped_total"] = r.dropped.Load()
	params["telemetry_failed_total"] = r.failed.Load()

	sendCtx, cancel := context.WithTimeout(ctx, sendTimeout)
	defer cancel()
	if err := r.sender(sendCtx, ev.Name, params); err != nil {
		n := r.failed.Add(1)
		slog.Warn(logPrefix+" delivery failed", "component", "telemetry",
			"event_name", ev.Name, "event_id", ev.ID, "error", err, "failed_total", n)
		return
	}
	level := slog.LevelDebug
	if strings.HasPrefix(ev.Name, "voice_metrics_") || strings.HasPrefix(ev.Name, "chat_metrics_") || strings.HasPrefix(ev.Name, "sensing_metrics_") {
		// Task KPI verification needs an observable acceptance receipt.
		level = slog.LevelInfo
	}
	slog.Log(ctx, level, logPrefix+" delivered", "component", "telemetry",
		"event_name", ev.Name, "event_id", ev.ID)
}

// compactJSON renders params for logging, falling back to %v formatting.
func compactJSON(params map[string]any) string {
	b, err := json.Marshal(params)
	if err != nil {
		return "unserializable"
	}
	return string(b)
}
