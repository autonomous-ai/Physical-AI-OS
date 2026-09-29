// Package flow emits structured flow events to daily JSONL, an in-memory ring and the monitor bus.
// Example: t := flow.Start("sensing_input", data); flow.End("sensing_input", t, data)
package flow

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/monitor"
)

// Kind is the flow event kind.
type Kind string

const (
	KindEnter Kind = "enter" // node activated (Start)
	KindExit  Kind = "exit"  // node completed with duration (End)
	KindEvent Kind = "event" // one-shot observation (Log)
)

// Event is one structured flow record persisted to JSONL.
type Event struct {
	Kind       Kind           `json:"kind"`
	Node       string         `json:"node"`
	TS         float64        `json:"ts"` // Unix seconds
	Seq        int64          `json:"seq"`
	TraceID    string         `json:"trace_id,omitempty"`
	DurationMs int64          `json:"duration_ms,omitempty"` // exit only
	Data       map[string]any `json:"data,omitempty"`
	Version    string         `json:"version,omitempty"`
}

const (
	ringSize      = 200
	logsDir       = "local"
	retentionDays = 7
)

type emitter struct {
	mu               sync.Mutex
	seqN             atomic.Int64
	ring             []Event
	file             *os.File
	day              string // YYYY-MM-DD of current log file
	traceID          string // active turn trace ID (serialized per turn)
	traceActiveCount int    // reference count for the active trace
	bus              *monitor.Bus
	version          string // injected at Init, stamped on every event
}

var global = &emitter{}

// Init attaches bus for SSE broadcast and stamps version on every event; call once at startup.
func Init(bus *monitor.Bus, version string) {
	global.mu.Lock()
	global.bus = bus
	global.version = version
	global.mu.Unlock()
	_ = os.MkdirAll(logsDir, 0o755)
	go cleanOldLogs()
}

// cleanOldLogs removes flow_events_*.jsonl files older than retentionDays.
func cleanOldLogs() {
	entries, err := os.ReadDir(logsDir)
	if err != nil {
		return
	}
	cutoff := time.Now().AddDate(0, 0, -retentionDays).Format("2006-01-02")
	for _, e := range entries {
		name := e.Name()
		if !strings.HasPrefix(name, "flow_events_") || !strings.HasSuffix(name, ".jsonl") {
			continue
		}
		date := strings.TrimSuffix(strings.TrimPrefix(name, "flow_events_"), ".jsonl")
		if date < cutoff {
			path := filepath.Join(logsDir, name)
			if err := os.Remove(path); err == nil {
				slog.Info("removed old flow log", "component", "flow", "file", name)
			}
		}
	}
}

// Start emits an "enter" event for node and returns the start time for use with End.
// Optional runID overrides the global trace for this event.
func Start(node string, data map[string]any, runID ...string) time.Time {
	t := time.Now()
	global.emit(KindEnter, node, 0, data, firstStr(runID))
	return t
}

// End emits an "exit" event for node with duration since startTime.
// Optional runID overrides the global trace for this event.
func End(node string, startTime time.Time, data map[string]any, runID ...string) {
	global.emit(KindExit, node, time.Since(startTime).Milliseconds(), data, firstStr(runID))
}

// Log emits a one-shot "event" observation.
// Optional runID overrides the global trace for this event.
func Log(node string, data map[string]any, runID ...string) {
	global.emit(KindEvent, node, 0, data, firstStr(runID))
}

func firstStr(ss []string) string {
	if len(ss) > 0 {
		return ss[0]
	}
	return ""
}

// SetTrace sets the fallback trace ID for events without an explicit runID.
// Prefer passing runID to Start/End/Log; GetTrace() == "" means no device turn is active.
func SetTrace(id string) {
	global.mu.Lock()
	global.traceID = id
	global.traceActiveCount++
	global.mu.Unlock()
}

// ClearTrace clears the active trace ID (call when a turn ends).
func ClearTrace() {
	global.mu.Lock()
	if global.traceActiveCount > 0 {
		global.traceActiveCount--
	}
	if global.traceActiveCount == 0 {
		global.traceID = ""
	}
	global.mu.Unlock()
}

// GetTrace returns the current active trace ID, or "" if none is set.
func GetTrace() string {
	global.mu.Lock()
	defer global.mu.Unlock()
	if global.traceActiveCount == 0 {
		return ""
	}
	return global.traceID
}

// Recent returns up to n most recent events from the ring buffer.
func Recent(n int) []Event {
	global.mu.Lock()
	defer global.mu.Unlock()
	if n > len(global.ring) {
		n = len(global.ring)
	}
	out := make([]Event, n)
	copy(out, global.ring[len(global.ring)-n:])
	return out
}

func (e *emitter) emit(kind Kind, node string, durMs int64, data map[string]any, overrideRunID string) {
	now := time.Now()
	seq := e.seqN.Add(1)

	e.mu.Lock()
	traceID := e.traceID
	version := e.version
	e.mu.Unlock()

	if overrideRunID != "" {
		traceID = overrideRunID
	}

	evt := Event{
		Kind:       kind,
		Node:       node,
		TS:         float64(now.UnixNano()) / 1e9,
		Seq:        seq,
		TraceID:    traceID,
		DurationMs: durMs,
		Data:       data,
		Version:    version,
	}

	e.mu.Lock()
	e.ring = append(e.ring, evt)
	if len(e.ring) > ringSize {
		e.ring = e.ring[len(e.ring)-ringSize:]
	}
	e.writeJSONL(now, evt)
	bus := e.bus
	e.mu.Unlock()

	if bus != nil {
		summary := fmt.Sprintf("[%s] %s", kind, node)
		if durMs > 0 {
			summary += fmt.Sprintf(" (%dms)", durMs)
		}
		bus.Push(domain.MonitorEvent{
			Type:    "flow_" + string(kind),
			Summary: summary,
			RunID:   traceID,
			Phase:   string(kind),
			Detail:  map[string]any{"node": node, "dur_ms": durMs, "data": data},
		})
	}
}

// writeJSONL appends the event to the current day's JSONL file.
// Must be called with e.mu held.
func (e *emitter) writeJSONL(now time.Time, evt Event) {
	day := now.Format("2006-01-02")
	if e.day != day || e.file == nil {
		if e.file != nil {
			_ = e.file.Close()
		}
		name := filepath.Join(logsDir, fmt.Sprintf("flow_events_%s.jsonl", day))
		f, err := os.OpenFile(name, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
		if err != nil {
			return // storage may be read-only
		}
		e.file = f
		e.day = day
	}
	b, _ := json.Marshal(evt)
	_, _ = e.file.Write(append(b, '\n'))
}
