package environment

import (
	"fmt"
	"math"
	"testing"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

func testSettings() config.EnvironmentConfig {
	c := config.DefaultEnvironmentConfig()
	c.EvaluateIntervalS = 10
	c.SustainS = 20
	c.CooldownS = 60
	c.RetryIntervalS = 10
	c.Metrics = map[string]config.EnvironmentMetricRule{"temperature_c": {Delta: 2, WarmupS: 20}}
	return c
}
func number(v float64) *float64 { return &v }

var epoch = time.Unix(1000, 0)

func sample(second int, value float64) Snapshot {
	return Snapshot{State: "ready", Enabled: true, AgeS: number(0), Sample: map[string]*float64{"timestamp": number(float64(1000 + second)), "temperature_c": number(value)}}
}
func observe(d *Detector, second int, value float64) *Event {
	return d.Observe(epoch.Add(time.Duration(second)*time.Second), sample(second, value))
}
func mustNoEvent(t *testing.T, e *Event) {
	t.Helper()
	if e != nil {
		t.Fatalf("unexpected event: %+v", e)
	}
}
func mustChange(t *testing.T, e *Event, previous, current float64) {
	t.Helper()
	if e == nil {
		t.Fatal("missing event")
	}
	v := e.Changes["temperature_c"]
	if v.Previous != previous || v.Current != current || v.Delta != current-previous {
		t.Fatalf("change: %+v", v)
	}
}

func TestDetectorWarmupSustainAndSpikeReset(t *testing.T) {
	d := NewDetector(testSettings())
	for _, row := range []struct {
		second int
		value  float64
	}{{0, 10}, {10, 30}, {20, 20}, {30, 23}, {40, 20}, {50, 23}, {60, 23}} {
		mustNoEvent(t, observe(d, row.second, row.value))
	}
	mustChange(t, observe(d, 70, 23), 20, 23)
}
func TestDetectorReversalRestartsSustain(t *testing.T) {
	d := NewDetector(testSettings())
	for _, row := range []struct {
		second int
		value  float64
	}{{0, 20}, {10, 20}, {20, 20}, {30, 23}, {40, 17}, {50, 17}} {
		mustNoEvent(t, observe(d, row.second, row.value))
	}
	mustChange(t, observe(d, 60, 17), 20, 17)
}
func TestDetectorDroppedRetryAndAcceptedBaselineCooldown(t *testing.T) {
	d := NewDetector(testSettings())
	for second := 0; second <= 20; second += 10 {
		mustNoEvent(t, observe(d, second, 20))
	}
	mustNoEvent(t, observe(d, 30, 23))
	mustNoEvent(t, observe(d, 40, 23))
	event := observe(d, 50, 23)
	mustChange(t, event, 20, 23)
	d.Attempted(epoch.Add(50*time.Second), event, false)
	mustNoEvent(t, observe(d, 55, 23))
	event = observe(d, 60, 23)
	mustChange(t, event, 20, 23)
	d.Attempted(epoch.Add(60*time.Second), event, true)
	mustNoEvent(t, observe(d, 70, 23))
	for second := 80; second <= 110; second += 10 {
		mustNoEvent(t, observe(d, second, 20))
	}
	mustChange(t, observe(d, 120, 20), 23, 20)
}
func TestDetectorInvalidReadingsResetWarmup(t *testing.T) {
	cases := map[string]func(*Snapshot){
		"device fault": func(s *Snapshot) { s.Sample["device_status"] = number(1) },
		"disabled":     func(s *Snapshot) { s.Enabled = false }, "error": func(s *Snapshot) { s.State = "error" },
		"stale": func(s *Snapshot) { s.Stale = true }, "old": func(s *Snapshot) { s.AgeS = number(11) },
		"unknown age": func(s *Snapshot) { s.AgeS = nil }, "negative age": func(s *Snapshot) { s.AgeS = number(-1) },
		"null sample": func(s *Snapshot) { s.Sample = nil }, "null metric": func(s *Snapshot) { s.Sample["temperature_c"] = nil },
		"invalid metric":    func(s *Snapshot) { s.Sample["temperature_c"] = number(math.NaN()) },
		"invalid timestamp": func(s *Snapshot) { s.Sample["timestamp"] = number(math.Inf(1)) },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			d := NewDetector(testSettings())
			for second := 0; second <= 20; second += 10 {
				observe(d, second, 20)
			}
			observe(d, 30, 23)
			bad := sample(40, 23)
			mutate(&bad)
			mustNoEvent(t, d.Observe(epoch.Add(40*time.Second), bad))
			for second := 50; second <= 70; second += 10 {
				mustNoEvent(t, observe(d, second, 23))
			}
			mustNoEvent(t, observe(d, 80, 26))
			mustNoEvent(t, observe(d, 90, 26))
			mustChange(t, observe(d, 100, 26), 23, 26)
		})
	}
}
func TestDetectorOutageRecoveryAndRepeatedSamples(t *testing.T) {
	d := NewDetector(testSettings())
	for second := 0; second <= 20; second += 10 {
		observe(d, second, 20)
	}
	observe(d, 30, 23)
	mustNoEvent(t, d.Observe(epoch.Add(40*time.Second), sample(30, 23)))
	// A repeated timestamp cannot count as an independent sustained observation.
	mustNoEvent(t, d.Observe(epoch.Add(50*time.Second), sample(30, 23)))
	// Long gap invalidates prior baseline, even if the next sample looks fresh.
	for second := 100; second <= 120; second += 10 {
		mustNoEvent(t, observe(d, second, 23))
	}
	mustNoEvent(t, observe(d, 130, 26))
	mustNoEvent(t, observe(d, 140, 26))
	mustChange(t, observe(d, 150, 26), 23, 26)
}
func TestDetectorNullMetricDoesNotResetOtherMetrics(t *testing.T) {
	c := testSettings()
	c.Metrics["humidity_pct"] = config.EnvironmentMetricRule{Delta: 10, WarmupS: 0}
	d := NewDetector(c)
	for second := 0; second <= 20; second += 10 {
		s := sample(second, 20)
		s.Sample["humidity_pct"] = number(50)
		mustNoEvent(t, d.Observe(epoch.Add(time.Duration(second)*time.Second), s))
	}
	for second := 30; second <= 50; second += 10 {
		s := sample(second, 23)
		s.Sample["humidity_pct"] = nil
		e := d.Observe(epoch.Add(time.Duration(second)*time.Second), s)
		if second < 50 {
			mustNoEvent(t, e)
		} else {
			mustChange(t, e, 20, 23)
		}
	}
}

func componentSample(second, co2Second int, temperature, co2 float64) Snapshot {
	s := sample(second, temperature)
	c := Snapshot{State: "ready", Enabled: true, AgeS: number(float64(second - co2Second)), Sample: map[string]*float64{"timestamp": number(float64(1000 + co2Second)), "co2_ppm": number(co2)}}
	s.Components = map[string]Snapshot{"sen55": sample(second, temperature), "scd41": c}
	s.Sample["co2_ppm"] = number(co2)
	s.Sources = map[string]string{"temperature_c": "sen55", "co2_ppm": "scd41"}
	s.MetricTimestamps = map[string]float64{"temperature_c": float64(1000 + second), "co2_ppm": float64(1000 + co2Second)}
	return s
}
func multiDetector() *Detector {
	c := testSettings()
	c.Metrics["temperature_c"] = config.EnvironmentMetricRule{Delta: 2}
	c.Metrics["co2_ppm"] = config.EnvironmentMetricRule{Delta: 200}
	return NewDetector(c)
}
func TestComponentFailureIsolated(t *testing.T) {
	for _, failed := range []string{"sen55", "scd41"} {
		t.Run(failed, func(t *testing.T) {
			d := multiDetector()
			d.Observe(epoch, componentSample(0, 0, 20, 500))
			var e *Event
			for second := 10; second <= 30; second += 10 {
				s := componentSample(second, second, 23, 800)
				c := s.Components[failed]
				c.Sample["device_status"] = number(1)
				s.Components[failed] = c
				e = d.Observe(epoch.Add(time.Duration(second)*time.Second), s)
			}
			if e == nil {
				t.Fatal("healthy component lost change")
			}
			good, bad := "temperature_c", "co2_ppm"
			if failed == "sen55" {
				good, bad = bad, good
			}
			if len(e.Changes) != 1 || e.Changes[good].Current == 0 || e.Sample[bad] != nil {
				t.Fatalf("event leaks failed component: %+v", e)
			}
		})
	}
}
func TestComponentTimestampCannotBorrowOtherSensorFreshness(t *testing.T) {
	d := multiDetector()
	d.cfg.MaxSampleAgeS = 60
	d.Observe(epoch, componentSample(0, 0, 20, 500))
	d.Observe(epoch.Add(10*time.Second), componentSample(10, 10, 20, 800))
	for second := 20; second <= 30; second += 10 {
		mustNoEvent(t, d.Observe(epoch.Add(time.Duration(second)*time.Second), componentSample(second, 10, 20, 800)))
	}
	e := d.Observe(epoch.Add(40*time.Second), componentSample(40, 35, 20, 800))
	if e == nil || e.Changes["co2_ppm"].CurrentAt != 1035 || e.MetricTimestamps["co2_ppm"] != 1035 || e.ObservedAt != 1040 || e.Changes["co2_ppm"].Source != "scd41" {
		t.Fatalf("wrong provenance: %+v", e)
	}
	d.Attempted(epoch.Add(40*time.Second), e, true)
	if d.metrics["co2_ppm"].baselineAt != 1035 {
		t.Fatal("baseline borrowed aggregate timestamp")
	}
}
func TestComponentInvalidProvenanceResetsOnlyMetric(t *testing.T) {
	for _, mutate := range []func(*Snapshot){
		func(s *Snapshot) { delete(s.Sources, "co2_ppm") },
		func(s *Snapshot) { s.MetricTimestamps["co2_ppm"]++ },
		func(s *Snapshot) { s.Sample["co2_ppm"] = number(999) },
		func(s *Snapshot) { c := s.Components["scd41"]; c.Stale = true; s.Components["scd41"] = c },
	} {
		d := multiDetector()
		d.Observe(epoch, componentSample(0, 0, 20, 500))
		var e *Event
		for second := 10; second <= 30; second += 10 {
			s := componentSample(second, second, 23, 800)
			mutate(&s)
			e = d.Observe(epoch.Add(time.Duration(second)*time.Second), s)
		}
		mustChange(t, e, 20, 23)
		if _, ok := e.Changes["co2_ppm"]; ok {
			t.Fatal("invalid CO2 emitted")
		}
	}
}
func TestComponentReplacementRestartsBaseline(t *testing.T) {
	d := multiDetector()
	d.Observe(epoch, componentSample(0, 0, 20, 500))
	for second := 10; second <= 30; second += 10 {
		s := componentSample(second, second, 20, 800)
		s.Components["replacement"] = s.Components["scd41"]
		s.Sources["co2_ppm"] = "replacement"
		mustNoEvent(t, d.Observe(epoch.Add(time.Duration(second)*time.Second), s))
	}
	if *d.metrics["co2_ppm"].baseline != 800 {
		t.Fatal("replacement inherited old baseline")
	}
}

func relativePMDetector() *Detector {
	c := testSettings()
	c.CooldownS = 0
	c.RetryIntervalS = 1
	c.Metrics = map[string]config.EnvironmentMetricRule{
		"pm2_5_ug_m3": {Delta: 10, RelativeDeltaPct: 20},
	}
	return NewDetector(c)
}

func observePM(d *Detector, second int, value float64) *Event {
	s := sample(second, 20)
	delete(s.Sample, "temperature_c")
	s.Sample["pm2_5_ug_m3"] = number(value)
	return d.Observe(epoch.Add(time.Duration(second)*time.Second), s)
}

func mustPMChange(t *testing.T, e *Event, previous, current float64) {
	t.Helper()
	if e == nil {
		t.Fatal("missing PM change")
	}
	change, ok := e.Changes["pm2_5_ug_m3"]
	if !ok || len(e.Changes) != 1 || change.Previous != previous || change.Current != current || change.Delta != current-previous {
		t.Fatalf("unexpected PM change: %+v", e)
	}
}

func TestDetectorRelativeThresholdUsesBaselineInBothDirections(t *testing.T) {
	for _, current := range []float64{240, 160} {
		t.Run(fmt.Sprintf("current_%g", current), func(t *testing.T) {
			d := relativePMDetector()
			mustNoEvent(t, observePM(d, 0, 200))
			for _, row := range []struct {
				second int
				value  float64
			}{{10, 220}, {20, 239}, {30, 239}, {40, 239}, {50, current}, {60, current}} {
				mustNoEvent(t, observePM(d, row.second, row.value))
			}
			mustPMChange(t, observePM(d, 70, current), 200, current)
		})
	}
}

func TestDetectorRelativeThresholdKeepsAbsoluteFloor(t *testing.T) {
	for _, baseline := range []float64{0, 5} {
		t.Run(fmt.Sprintf("baseline_%g", baseline), func(t *testing.T) {
			d := relativePMDetector()
			mustNoEvent(t, observePM(d, 0, baseline))
			for second := 10; second <= 30; second += 10 {
				mustNoEvent(t, observePM(d, second, baseline+9))
			}
			mustNoEvent(t, observePM(d, 40, baseline+10))
			mustNoEvent(t, observePM(d, 50, baseline+10))
			mustPMChange(t, observePM(d, 60, baseline+10), baseline, baseline+10)
		})
	}
}

func TestDetectorRelativeThresholdReanchorsOnlyAfterAcceptedDispatch(t *testing.T) {
	for _, accepted := range []bool{true, false} {
		t.Run(fmt.Sprintf("accepted_%t", accepted), func(t *testing.T) {
			d := relativePMDetector()
			mustNoEvent(t, observePM(d, 0, 200))
			mustNoEvent(t, observePM(d, 10, 400))
			mustNoEvent(t, observePM(d, 20, 400))
			event := observePM(d, 30, 400)
			mustPMChange(t, event, 200, 400)
			d.Attempted(epoch.Add(30*time.Second), event, accepted)
			if !accepted {
				// The rejected event must not raise the threshold or advance the baseline.
				mustPMChange(t, observePM(d, 40, 240), 200, 240)
				return
			}
			for second := 40; second <= 60; second += 10 {
				mustNoEvent(t, observePM(d, second, 460))
			}
			mustNoEvent(t, observePM(d, 70, 480))
			mustNoEvent(t, observePM(d, 80, 480))
			mustPMChange(t, observePM(d, 90, 480), 400, 480)
		})
	}
}

func TestDetectorRelativeThresholdResetsSustain(t *testing.T) {
	for _, resetValue := range []float64{239, 160} {
		t.Run(fmt.Sprintf("reset_%g", resetValue), func(t *testing.T) {
			d := relativePMDetector()
			mustNoEvent(t, observePM(d, 0, 200))
			mustNoEvent(t, observePM(d, 10, 240))
			mustNoEvent(t, observePM(d, 20, resetValue))
			mustNoEvent(t, observePM(d, 30, 240))
			mustNoEvent(t, observePM(d, 40, 240))
			mustPMChange(t, observePM(d, 50, 240), 200, 240)
		})
	}
}

func comfortDetector() *Detector {
	c := testSettings()
	c.CooldownS = 0
	c.Metrics = map[string]config.EnvironmentMetricRule{
		"temperature_c": {
			Delta:   100,
			Comfort: &config.EnvironmentComfortRule{Below: number(19), Above: number(27), Hysteresis: 1, SustainS: 20},
		},
	}
	return NewDetector(c)
}

func mustComfort(t *testing.T, event *Event, previous, state string, current, threshold float64) {
	t.Helper()
	if event == nil {
		t.Fatal("missing comfort event")
	}
	change, ok := event.Comfort["temperature_c"]
	if !ok || change.PreviousState != previous || change.State != state || change.Current != current || change.Threshold != threshold || change.SustainedS < 20 {
		t.Fatalf("unexpected comfort: %+v", event)
	}
}

func TestComfortStableHighWithoutDeltaOrInitialConsumption(t *testing.T) {
	d := comfortDetector()
	mustNoEvent(t, observe(d, 0, 30))
	// An initial dispatch updates only the delta baseline, not the comfort timer.
	initial := &Event{Reason: "initial", Sample: map[string]*float64{"temperature_c": number(30)}, MetricTimestamps: map[string]float64{"temperature_c": 1000}}
	d.Attempted(epoch, initial, true)
	mustNoEvent(t, observe(d, 10, 30))
	e := observe(d, 20, 30)
	mustComfort(t, e, "normal", "high", 30, 27)
	if len(e.Changes) != 0 {
		t.Fatalf("stationary room emitted a delta: %+v", e)
	}
	d.Attempted(epoch.Add(20*time.Second), e, true)
	for second := 30; second <= 90; second += 10 {
		mustNoEvent(t, observe(d, second, 30))
	}
}

func TestComfortBoundariesAndSustainReset(t *testing.T) {
	for _, tc := range []struct {
		name                string
		boundary, excursion float64
		state               string
	}{{"hot", 27, 28, "high"}, {"cold", 19, 18, "low"}} {
		t.Run(tc.name, func(t *testing.T) {
			d := comfortDetector()
			for second := 0; second <= 20; second += 10 {
				mustNoEvent(t, observe(d, second, tc.boundary))
			}
			mustNoEvent(t, observe(d, 30, tc.excursion))
			mustNoEvent(t, observe(d, 40, tc.boundary))
			mustNoEvent(t, observe(d, 50, tc.excursion))
			mustNoEvent(t, observe(d, 60, tc.excursion))
			mustComfort(t, observe(d, 70, tc.excursion), "normal", tc.state, tc.excursion, tc.boundary)
		})
	}
}

func TestComfortHysteresisRecoveryAndRearm(t *testing.T) {
	d := comfortDetector()
	observe(d, 0, 30)
	observe(d, 10, 30)
	e := observe(d, 20, 30)
	mustComfort(t, e, "normal", "high", 30, 27)
	d.Attempted(epoch.Add(20*time.Second), e, true)
	// Entering the band above the recovery boundary cannot clear the high state.
	for second := 30; second <= 50; second += 10 {
		mustNoEvent(t, observe(d, second, 26.5))
	}
	mustNoEvent(t, observe(d, 60, 26))
	mustNoEvent(t, observe(d, 70, 26.5))
	mustNoEvent(t, observe(d, 80, 26))
	mustNoEvent(t, observe(d, 90, 26))
	e = observe(d, 100, 26)
	mustComfort(t, e, "high", "recovered", 26, 26)
	d.Attempted(epoch.Add(100*time.Second), e, true)
	mustNoEvent(t, observe(d, 110, 28))
	mustNoEvent(t, observe(d, 120, 28))
	e = observe(d, 130, 28)
	mustComfort(t, e, "normal", "high", 28, 27)
	d.Attempted(epoch.Add(130*time.Second), e, true)
	// The opposite extreme transitions directly, without a spurious recovery.
	mustNoEvent(t, observe(d, 140, 18))
	mustNoEvent(t, observe(d, 150, 18))
	e = observe(d, 160, 18)
	mustComfort(t, e, "high", "low", 18, 19)
	d.Attempted(epoch.Add(160*time.Second), e, true)
	mustNoEvent(t, observe(d, 170, 19.5))
	mustNoEvent(t, observe(d, 180, 20))
	mustNoEvent(t, observe(d, 190, 20))
	mustComfort(t, observe(d, 200, 20), "low", "recovered", 20, 20)
}

func TestComfortDroppedDispatchRetriesWithoutAcknowledging(t *testing.T) {
	d := comfortDetector()
	observe(d, 0, 30)
	observe(d, 10, 30)
	e := observe(d, 20, 30)
	mustComfort(t, e, "normal", "high", 30, 27)
	d.Attempted(epoch.Add(20*time.Second), e, false)
	mustNoEvent(t, observe(d, 25, 30))
	e = observe(d, 30, 30)
	mustComfort(t, e, "normal", "high", 30, 27)
	d.Attempted(epoch.Add(30*time.Second), e, true)
	mustNoEvent(t, observe(d, 40, 30))
}

func TestComfortFreshnessAndSourceReset(t *testing.T) {
	for _, mode := range []string{"stale", "source", "gap"} {
		t.Run(mode, func(t *testing.T) {
			d := comfortDetector()
			d.Observe(epoch, componentSample(0, 0, 30, 500))
			d.Observe(epoch.Add(10*time.Second), componentSample(10, 10, 30, 500))
			start := 20
			if mode == "stale" {
				s := componentSample(20, 20, 30, 500)
				owner := s.Components["sen55"]
				owner.Stale = true
				s.Components["sen55"] = owner
				mustNoEvent(t, d.Observe(epoch.Add(20*time.Second), s))
				start = 30
			} else if mode == "gap" {
				start = 40
			}
			for second := start; second <= start+20; second += 10 {
				s := componentSample(second, second, 30, 500)
				if mode == "source" {
					s.Components["replacement"] = s.Components["sen55"]
					s.Sources["temperature_c"] = "replacement"
				}
				e := d.Observe(epoch.Add(time.Duration(second)*time.Second), s)
				if second < start+20 {
					mustNoEvent(t, e)
				} else {
					mustComfort(t, e, "normal", "high", 30, 27)
					if mode == "source" && e.Comfort["temperature_c"].Source != "replacement" {
						t.Fatalf("incorrect comfort source: %+v", e)
					}
				}
			}
		})
	}
}

func TestComfortAndDeltaShareDispatchGates(t *testing.T) {
	d := comfortDetector()
	rule := d.cfg.Metrics["temperature_c"]
	rule.Delta = 2
	d.cfg.Metrics["temperature_c"] = rule
	d.cfg.CooldownS = 60
	observe(d, 0, 25)
	mustNoEvent(t, observe(d, 10, 30))
	mustNoEvent(t, observe(d, 20, 30))
	e := observe(d, 30, 30)
	mustChange(t, e, 25, 30)
	mustComfort(t, e, "normal", "high", 30, 27)
	d.Attempted(epoch.Add(30*time.Second), e, true)
	for second := 40; second < 90; second += 10 {
		mustNoEvent(t, observe(d, second, 25))
	}
	e = observe(d, 90, 25)
	mustChange(t, e, 30, 25)
	mustComfort(t, e, "high", "recovered", 25, 26)
}

func TestDeltaDispatchDoesNotConsumeComfortCandidate(t *testing.T) {
	d := comfortDetector()
	rule := d.cfg.Metrics["temperature_c"]
	rule.Delta = 2
	rule.Comfort.SustainS = 40
	d.cfg.Metrics["temperature_c"] = rule
	observe(d, 0, 25)
	observe(d, 10, 30)
	observe(d, 20, 30)
	e := observe(d, 30, 30)
	mustChange(t, e, 25, 30)
	if len(e.Comfort) != 0 {
		t.Fatal("comfort triggered before its longer sustain")
	}
	d.Attempted(epoch.Add(30*time.Second), e, true)
	mustNoEvent(t, observe(d, 40, 30))
	e = observe(d, 50, 30)
	mustComfort(t, e, "normal", "high", 30, 27)
	if len(e.Changes) != 0 {
		t.Fatal("comfort dispatch should not repeat acknowledged delta")
	}
}
