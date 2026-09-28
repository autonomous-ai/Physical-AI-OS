// Package environment turns fresh HAL snapshots into sustained-change events.
package environment

import (
	"encoding/json"
	"math"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

type Snapshot struct {
	Timing struct {
		StaleAfterS float64 `json:"stale_after_s"`
	} `json:"timing,omitempty"`
	State            string              `json:"state"`
	Enabled          bool                `json:"enabled"`
	Stale            bool                `json:"stale"`
	ContinuousDataS  *float64            `json:"continuous_data_s,omitempty"`
	AgeS             *float64            `json:"age_s"`
	Sample           map[string]*float64 `json:"sample"`
	Components       map[string]Snapshot `json:"components,omitempty"`
	Sources          map[string]string   `json:"sources,omitempty"`
	MetricTimestamps map[string]float64  `json:"metric_timestamps,omitempty"`
}

type Change struct {
	CurrentAt  float64 `json:"current_at"`
	Source     string  `json:"source,omitempty"`
	Previous   float64 `json:"previous"`
	PreviousAt float64 `json:"previous_at"`
	Current    float64 `json:"current"`
	Delta      float64 `json:"delta"`
}

type Event struct {
	Reason           string                   `json:"reason,omitempty"`
	ObservedAt       float64                  `json:"observed_at"`
	Sample           map[string]*float64      `json:"sample"`
	Changes          map[string]Change        `json:"changes"`
	Comfort          map[string]ComfortChange `json:"comfort,omitempty"`
	SustainedS       float64                  `json:"sustained_s"`
	Sources          map[string]string        `json:"sources,omitempty"`
	MetricTimestamps map[string]float64       `json:"metric_timestamps,omitempty"`
}

// ComfortChange reports a sustained product comfort band, not a health alarm.
type ComfortChange struct {
	State         string  `json:"state"`
	PreviousState string  `json:"previous_state"`
	Current       float64 `json:"current"`
	CurrentAt     float64 `json:"current_at"`
	Threshold     float64 `json:"threshold"`
	SustainedS    float64 `json:"sustained_s"`
	Source        string  `json:"source,omitempty"`
}

func (e Event) Message() string {
	body, _ := json.Marshal(e)
	return string(body)
}

type metricState struct {
	source           string
	lastSample       float64
	lastFresh        time.Time
	first            time.Time
	baseline         *float64
	baselineAt       float64
	since            time.Time
	direction        int
	ready            bool
	continuous       *float64
	comfortState     string
	comfortCandidate string
	comfortSince     time.Time
}

func (s *metricState) observeComfort(now time.Time, value, stamp float64, rule *config.EnvironmentComfortRule) *ComfortChange {
	if rule == nil {
		return nil
	}
	if s.comfortState == "" {
		s.comfortState = "normal"
	}
	target := s.comfortState
	var threshold float64
	switch {
	case rule.Above != nil && value > *rule.Above:
		target, threshold = "high", *rule.Above
	case rule.Below != nil && value < *rule.Below:
		target, threshold = "low", *rule.Below
	case s.comfortState == "high" && rule.Above != nil && value <= *rule.Above-rule.Hysteresis:
		target, threshold = "normal", *rule.Above-rule.Hysteresis
	case s.comfortState == "low" && rule.Below != nil && value >= *rule.Below+rule.Hysteresis:
		target, threshold = "normal", *rule.Below+rule.Hysteresis
	}
	if target == s.comfortState {
		s.comfortCandidate, s.comfortSince = "", time.Time{}
		return nil
	}
	if target != s.comfortCandidate || s.comfortSince.IsZero() {
		s.comfortCandidate, s.comfortSince = target, now
		return nil
	}
	if now.Sub(s.comfortSince).Seconds() < rule.SustainS {
		return nil
	}
	label := target
	if target == "normal" {
		label = "recovered"
	}
	return &ComfortChange{State: label, PreviousState: s.comfortState, Current: value, CurrentAt: stamp, Threshold: threshold, SustainedS: now.Sub(s.comfortSince).Seconds(), Source: s.source}
}

// Detector is owned by one polling goroutine. Baselines change only after the
// event was accepted by the dispatch layer, never after a dropped HTTP 200.
type Detector struct {
	cfg          config.EnvironmentConfig
	metrics      map[string]*metricState
	lastAccepted time.Time
	lastAttempt  time.Time
}

func NewDetector(cfg config.EnvironmentConfig) *Detector {
	return &Detector{cfg: cfg.Clone(), metrics: make(map[string]*metricState)}
}

func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }

func (d *Detector) ResetReadings() {
	d.metrics = make(map[string]*metricState)
}

// reading validates freshness against the producing component, never the newest
// timestamp of a different sensor. Legacy single-sensor snapshots remain valid.
func (d *Detector) reading(s Snapshot, key string) (value *float64, stamp float64, source string) {
	owner := s
	if s.Components != nil {
		source = s.Sources[key]
		var ok bool
		owner, ok = s.Components[source]
		if !ok || source == "" {
			return nil, 0, source
		}
	}
	ts := owner.Sample["timestamp"]
	value = s.Sample[key]
	status := owner.Sample["device_status"]
	if !s.Enabled || s.State != "ready" || s.Stale || !owner.Enabled || owner.State != "ready" || owner.Stale || owner.AgeS == nil || !finite(*owner.AgeS) || *owner.AgeS < 0 || *owner.AgeS > d.cfg.MaxSampleAgeS || ts == nil || !finite(*ts) || *ts <= 0 || value == nil || !finite(*value) || (status != nil && (!finite(*status) || *status != 0)) {
		return nil, 0, source
	}
	if s.Components != nil {
		metricStamp, ok := s.MetricTimestamps[key]
		componentValue := owner.Sample[key]
		if !ok || metricStamp != *ts || componentValue == nil || *componentValue != *value {
			return nil, 0, source
		}
	}
	return value, *ts, source
}

func (d *Detector) Observe(now time.Time, s Snapshot) *Event {
	event := &Event{Sample: make(map[string]*float64), Changes: make(map[string]Change), SustainedS: d.cfg.SustainS, Sources: make(map[string]string), MetricTimestamps: make(map[string]float64)}
	for key := range s.Sample {
		if key == "timestamp" || key == "device_status" {
			continue
		}
		value, stamp, source := d.reading(s, key)
		event.Sample[key] = value
		if value != nil {
			event.MetricTimestamps[key] = stamp
			if source != "" {
				event.Sources[key] = source
			}
			if stamp > event.ObservedAt {
				event.ObservedAt = stamp
			}
		}
	}
	event.Sample["timestamp"] = &event.ObservedAt
	for key, rule := range d.cfg.Metrics {
		value, stamp, source := d.reading(s, key)
		if value == nil {
			delete(d.metrics, key)
			continue
		}
		owner := s
		if s.Components != nil {
			owner = s.Components[source]
		}
		continuous := owner.ContinuousDataS
		if continuous != nil && (!finite(*continuous) || *continuous < 0) {
			continuous = nil
		}
		state := d.metrics[key]
		// Each source owns its warmup and outage history. One healthy component must
		// not preserve another component's baseline across a fault or replacement.
		if state == nil || state.source != source || (continuous != nil && state.continuous != nil && *continuous < *state.continuous) || (!state.lastFresh.IsZero() && now.Sub(state.lastFresh).Seconds() > math.Max(2*d.cfg.EvaluateIntervalS, d.cfg.MaxSampleAgeS)) || stamp < state.lastSample {
			state = &metricState{first: now, source: source}
			d.metrics[key] = state
		}
		if stamp <= state.lastSample {
			continue
		}
		state.lastSample, state.lastFresh = stamp, now
		if continuous != nil {
			v := *continuous
			state.continuous = &v
		}

		duration := now.Sub(state.first).Seconds()
		if continuous != nil {
			duration = math.Max(duration, *continuous)
		}
		state.ready = duration >= rule.WarmupS
		if !state.ready {
			continue
		}
		// Comfort also evaluates the first baseline and readings below the delta
		// gate, so a room that starts and stays uncomfortable is not overlooked.
		if change := state.observeComfort(now, *value, stamp, rule.Comfort); change != nil {
			if event.Comfort == nil {
				event.Comfort = make(map[string]ComfortChange)
			}
			event.Comfort[key] = *change
		}
		if state.baseline == nil {
			v := *value
			state.baseline = &v
			state.baselineAt = stamp
			continue
		}
		delta := *value - *state.baseline
		if math.Abs(delta) < rule.ChangeThreshold(*state.baseline) {
			state.since = time.Time{}
			state.direction = 0
			continue
		}
		direction := 1
		if delta < 0 {
			direction = -1
		}
		if state.since.IsZero() || direction != state.direction {
			state.since, state.direction = now, direction
			continue
		}
		if now.Sub(state.since).Seconds() >= d.cfg.SustainS {
			event.Changes[key] = Change{CurrentAt: stamp, Source: source, Previous: *state.baseline, PreviousAt: state.baselineAt, Current: *value, Delta: delta}
		}
	}
	if (len(event.Changes) == 0 && len(event.Comfort) == 0) || (!d.lastAccepted.IsZero() && now.Sub(d.lastAccepted).Seconds() < d.cfg.CooldownS) || (!d.lastAttempt.IsZero() && now.Sub(d.lastAttempt).Seconds() < d.cfg.RetryIntervalS) {
		return nil
	}
	return event
}

func (d *Detector) Attempted(now time.Time, event *Event, accepted bool) {
	d.lastAttempt = now
	if !accepted {
		return
	}
	d.lastAccepted = now
	if event.Reason == "initial" {
		for key, value := range event.Sample {
			state := d.metrics[key]
			if state == nil || value == nil || state.source != event.Sources[key] || state.baselineAt > event.MetricTimestamps[key] {
				continue
			}
			v := *value
			state.baseline, state.baselineAt = &v, event.MetricTimestamps[key]
			state.since, state.direction = time.Time{}, 0
		}
		return
	}
	for key, change := range event.Changes {
		if state := d.metrics[key]; state != nil {
			v := change.Current
			state.baseline = &v
			state.baselineAt = change.CurrentAt
			state.since = time.Time{}
			state.direction = 0
		}
	}
	for key, change := range event.Comfort {
		state := d.metrics[key]
		if state == nil || state.source != change.Source {
			continue
		}
		state.comfortState = change.State
		if change.State == "recovered" {
			state.comfortState = "normal"
		}
		state.comfortCandidate, state.comfortSince = "", time.Time{}
	}
}
