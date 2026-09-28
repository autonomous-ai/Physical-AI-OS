package environment

import (
	"sync"
	"time"
)

// StartupCoordinator coordinates the greeting and polling worker; one startup report per process.
type StartupCoordinator struct {
	mu           sync.Mutex
	enabled      bool
	resolved     bool
	consumed     bool
	reserved     *Event
	cached       *Event
	expires      map[string]time.Time
	acknowledged *Event
	lastAttempt  time.Time
}

func NewStartupCoordinator() *StartupCoordinator { return &StartupCoordinator{} }

// ReserveGreeting returns only cached, stabilized readings that are fresh now.
// FinishGreeting must be called even when this returns nil or greeting is skipped.
func (c *StartupCoordinator) ReserveGreeting(now time.Time) *Event {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.enabled || c.resolved || c.consumed || c.reserved != nil {
		return nil
	}
	c.reserved = c.fresh(now)
	return cloneEvent(c.reserved)
}

// FinishGreeting releases the worker. Success consumes only readings actually
// reserved for this greeting; a plain greeting leaves the initial report pending.
func (c *StartupCoordinator) FinishGreeting(success bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.resolved {
		return
	}
	c.resolved = true
	if success && c.reserved != nil {
		c.consumed = true
		c.acknowledged = c.reserved
	}
	c.reserved = nil
}

func cloneEvent(e *Event) *Event {
	if e == nil {
		return nil
	}
	copy := *e
	copy.Sample = make(map[string]*float64)
	copy.Sources = make(map[string]string)
	copy.MetricTimestamps = make(map[string]float64)
	copy.Changes = make(map[string]Change)
	for k, v := range e.Sample {
		if v != nil {
			n := *v
			copy.Sample[k] = &n
		}
	}
	for k, v := range e.Sources {
		copy.Sources[k] = v
	}
	for k, v := range e.MetricTimestamps {
		copy.MetricTimestamps[k] = v
	}
	for k, v := range e.Changes {
		copy.Changes[k] = v
	}
	return &copy
}

// fresh is called under mu and expires each metric independently.
func (c *StartupCoordinator) fresh(now time.Time) *Event {
	event := cloneEvent(c.cached)
	if event == nil {
		return nil
	}
	event.ObservedAt = 0
	for key := range event.MetricTimestamps {
		if now.After(c.expires[key]) {
			delete(event.Sample, key)
			delete(event.Sources, key)
			delete(event.MetricTimestamps, key)
		} else if stamp := event.MetricTimestamps[key]; stamp > event.ObservedAt {
			event.ObservedAt = stamp
		}
	}
	if len(event.MetricTimestamps) == 0 {
		return nil
	}
	event.Sample["timestamp"] = &event.ObservedAt
	return event
}

func (c *StartupCoordinator) update(enabled bool, event *Event, expires map[string]time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.enabled = enabled
	c.cached, c.expires = event, expires
}

func (c *StartupCoordinator) takeAcknowledged() *Event {
	c.mu.Lock()
	defer c.mu.Unlock()
	event := c.acknowledged
	c.acknowledged = nil
	return event
}

// initial returns whether normal changes must wait, and an initial event when
// greeting has finished, readings are ready, and the retry interval permits it.
func (c *StartupCoordinator) initial(now time.Time, retryS float64) (bool, *Event) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.acknowledged != nil {
		return true, nil
	}
	if !c.enabled || c.consumed {
		return false, nil
	}
	if !c.resolved || (!c.lastAttempt.IsZero() && now.Sub(c.lastAttempt).Seconds() < retryS) {
		return true, nil
	}
	event := c.fresh(now)
	if event != nil {
		c.lastAttempt = now
	}
	return true, event
}

func (c *StartupCoordinator) acceptedInitial() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.consumed = true
}

// startupSnapshot excludes warming and unknown metrics, retaining only readings
// whose configured warmup has completed. It is never used as a trend assertion.
func (d *Detector) startupSnapshot(now time.Time, s Snapshot) (*Event, map[string]time.Time) {
	event := &Event{Reason: "initial", Sample: make(map[string]*float64), Changes: make(map[string]Change), Sources: make(map[string]string), MetricTimestamps: make(map[string]float64)}
	expires := make(map[string]time.Time)
	for key, state := range d.metrics {
		value, stamp, source := d.reading(s, key)
		if !state.ready || value == nil || source != state.source {
			continue
		}
		owner := s
		if s.Components != nil {
			owner = s.Components[source]
		}
		v := *value
		event.Sample[key] = &v
		event.MetricTimestamps[key] = stamp
		if source != "" {
			event.Sources[key] = source
		}
		maxAge := d.cfg.MaxSampleAgeS
		if ttl := owner.Timing.StaleAfterS; finite(ttl) && ttl > 0 && ttl < maxAge {
			maxAge = ttl
		}
		expires[key] = now.Add(time.Duration((maxAge - *owner.AgeS) * float64(time.Second)))
		if stamp > event.ObservedAt {
			event.ObservedAt = stamp
		}
	}
	if len(event.MetricTimestamps) == 0 {
		return nil, nil
	}
	event.Sample["timestamp"] = &event.ObservedAt
	return event, expires
}
