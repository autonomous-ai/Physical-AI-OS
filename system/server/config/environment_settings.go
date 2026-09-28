package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
)

// EnvironmentMetricRule defines a change threshold, not a health limit.
type EnvironmentMetricRule struct {
	Delta            float64                 `json:"delta"`
	RelativeDeltaPct float64                 `json:"relative_delta_pct"`
	WarmupS          float64                 `json:"warmup_s"`
	Comfort          *EnvironmentComfortRule `json:"comfort,omitempty"`
}

// EnvironmentComfortRule describes a persistent comfort condition, not a
// health alarm or a time-weighted exposure limit. Bounds are strict on entry.
type EnvironmentComfortRule struct {
	Below      *float64 `json:"below,omitempty"`
	Above      *float64 `json:"above,omitempty"`
	Hysteresis float64  `json:"hysteresis"`
	SustainS   float64  `json:"sustain_s"`
}

func (r *EnvironmentComfortRule) UnmarshalJSON(data []byte) error {
	type plain EnvironmentComfortRule
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	for name, raw := range fields {
		if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
			return fmt.Errorf("environment comfort.%s cannot be null", name)
		}
	}
	var value plain
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&value); err != nil {
		return err
	}
	*r = EnvironmentComfortRule(value)
	return nil
}

// ChangeThreshold returns the fixed floor or a fraction of the acknowledged
// baseline, whichever is larger. This is a product notification gate, not an
// uncertainty interval; accuracy does not establish within-device temporal noise.
func (r EnvironmentMetricRule) ChangeThreshold(baseline float64) float64 {
	return math.Max(r.Delta, math.Abs(baseline)*(r.RelativeDeltaPct/100))
}

// EnvironmentConfig controls OS interpretation separately from HAL acquisition.
type EnvironmentConfig struct {
	Enabled           bool                             `json:"enabled"`
	InitialReport     bool                             `json:"initial_report"`
	EvaluateIntervalS float64                          `json:"evaluate_interval_s"`
	SustainS          float64                          `json:"sustain_s"`
	CooldownS         float64                          `json:"cooldown_s"`
	RetryIntervalS    float64                          `json:"retry_interval_s"`
	MaxSampleAgeS     float64                          `json:"max_sample_age_s"`
	Metrics           map[string]EnvironmentMetricRule `json:"metrics"`
}

func DefaultEnvironmentConfig() EnvironmentConfig {
	bound := func(value float64) *float64 { return &value }
	return EnvironmentConfig{
		Enabled: true, InitialReport: true, EvaluateIntervalS: 10, SustainS: 60, CooldownS: 1800,
		RetryIntervalS: 60, MaxSampleAgeS: 10,
		// Provisional notification policy, not sensor accuracy or health limits.
		// Rationale and field-validation plan: robots/lamp/docs/environment-sensing.md.
		Metrics: map[string]EnvironmentMetricRule{
			"pm1_0_ug_m3":   {Delta: 10, RelativeDeltaPct: 20, WarmupS: 60},
			"pm2_5_ug_m3":   {Delta: 10, RelativeDeltaPct: 20, WarmupS: 60, Comfort: &EnvironmentComfortRule{Above: bound(35), Hysteresis: 5, SustainS: 300}},
			"pm4_0_ug_m3":   {Delta: 25, RelativeDeltaPct: 25, WarmupS: 60},
			"pm10_ug_m3":    {Delta: 25, RelativeDeltaPct: 25, WarmupS: 60},
			"temperature_c": {Delta: 2, WarmupS: 60, Comfort: &EnvironmentComfortRule{Below: bound(19), Above: bound(27), Hysteresis: 1, SustainS: 300}},
			"humidity_pct":  {Delta: 10, WarmupS: 60, Comfort: &EnvironmentComfortRule{Below: bound(35), Above: bound(65), Hysteresis: 5, SustainS: 300}},
			"voc_index":     {Delta: 50, WarmupS: 3600},
			"nox_index":     {Delta: 20, WarmupS: 21600},
			"co2_ppm":       {Delta: 200, RelativeDeltaPct: 20, WarmupS: 60, Comfort: &EnvironmentComfortRule{Above: bound(1000), Hysteresis: 150, SustainS: 300}},
		},
	}
}

// UnmarshalJSON fills omitted top-level fields with defaults. A supplied metrics
// map replaces the default map, allowing an operator to monitor a subset.
// Supplied rules without relative_delta_pct or comfort retain legacy behavior.
func (c *EnvironmentConfig) UnmarshalJSON(data []byte) error {
	type plain EnvironmentConfig
	v := plain(DefaultEnvironmentConfig())
	// Decode maps from empty so omitted metrics can genuinely be disabled.
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	for name, raw := range fields {
		if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
			return fmt.Errorf("environment.%s cannot be null", name)
		}
	}
	if _, ok := fields["metrics"]; ok {
		v.Metrics = nil
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&v); err != nil {
		return err
	}
	if raw, ok := fields["metrics"]; ok {
		var rules map[string]map[string]json.RawMessage
		if err := json.Unmarshal(raw, &rules); err != nil {
			return err
		}
		for key, fields := range rules {
			rule := DefaultEnvironmentConfig().Metrics[key]
			// Existing persisted rules must not silently acquire new event gates.
			rule.Comfort = nil
			if _, supplied := fields["relative_delta_pct"]; !supplied {
				rule.RelativeDeltaPct = 0
			}
			for name, raw := range fields {
				if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
					return fmt.Errorf("environment.metrics.%s.%s cannot be null", key, name)
				}
			}
			if fields == nil {
				return fmt.Errorf("environment.metrics.%s cannot be null", key)
			}
			body, _ := json.Marshal(fields)
			if err := json.Unmarshal(body, &rule); err != nil {
				return err
			}
			v.Metrics[key] = rule
		}
	}
	*c = EnvironmentConfig(v)
	return c.Validate()
}

func (c EnvironmentConfig) Validate() error {
	for name, value := range map[string]float64{
		"evaluate_interval_s": c.EvaluateIntervalS, "sustain_s": c.SustainS,
		"retry_interval_s": c.RetryIntervalS, "max_sample_age_s": c.MaxSampleAgeS,
	} {
		if math.IsNaN(value) || math.IsInf(value, 0) || value < 1 || value > 86400 {
			return fmt.Errorf("environment.%s must be between 1 and 86400 seconds", name)
		}
	}
	if math.IsNaN(c.CooldownS) || math.IsInf(c.CooldownS, 0) || c.CooldownS < 0 || c.CooldownS > 604800 {
		return fmt.Errorf("environment.cooldown_s must be between 0 and 604800 seconds")
	}
	if c.SustainS < c.EvaluateIntervalS || c.RetryIntervalS < c.EvaluateIntervalS {
		return fmt.Errorf("environment sustain_s and retry_interval_s must be at least evaluate_interval_s")
	}
	known := DefaultEnvironmentConfig().Metrics
	if len(c.Metrics) == 0 {
		return fmt.Errorf("environment.metrics must contain at least one metric")
	}
	for name, rule := range c.Metrics {
		if _, ok := known[name]; !ok {
			return fmt.Errorf("unknown environment metric %q", name)
		}
		if math.IsNaN(rule.Delta) || math.IsInf(rule.Delta, 0) || rule.Delta <= 0 {
			return fmt.Errorf("environment metric %s delta must be finite and positive", name)
		}
		if math.IsNaN(rule.RelativeDeltaPct) || math.IsInf(rule.RelativeDeltaPct, 0) || rule.RelativeDeltaPct < 0 || rule.RelativeDeltaPct > 100 {
			return fmt.Errorf("environment metric %s relative_delta_pct must be between 0 and 100", name)
		}
		if math.IsNaN(rule.WarmupS) || math.IsInf(rule.WarmupS, 0) || rule.WarmupS < 0 || rule.WarmupS > 86400 {
			return fmt.Errorf("environment metric %s warmup_s must be between 0 and 86400", name)
		}
		if rule.Comfort != nil {
			r := rule.Comfort
			if r.Below == nil && r.Above == nil {
				return fmt.Errorf("environment metric %s comfort requires below or above", name)
			}
			for _, bound := range []*float64{r.Below, r.Above} {
				if bound != nil && (math.IsNaN(*bound) || math.IsInf(*bound, 0)) {
					return fmt.Errorf("environment metric %s comfort bounds must be finite", name)
				}
			}
			if math.IsNaN(r.Hysteresis) || math.IsInf(r.Hysteresis, 0) || r.Hysteresis <= 0 {
				return fmt.Errorf("environment metric %s comfort hysteresis must be finite and positive", name)
			}
			if r.Below != nil && r.Above != nil && (*r.Below >= *r.Above || r.Hysteresis > (*r.Above-*r.Below)/2) {
				return fmt.Errorf("environment metric %s comfort bounds must be ordered with non-overlapping recovery bands", name)
			}
			if math.IsNaN(r.SustainS) || math.IsInf(r.SustainS, 0) || r.SustainS < c.EvaluateIntervalS || r.SustainS > 86400 {
				return fmt.Errorf("environment metric %s comfort sustain_s must be between evaluate_interval_s and 86400", name)
			}
		}

	}
	return nil
}

func (c EnvironmentConfig) Clone() EnvironmentConfig {
	copy := c
	copy.Metrics = make(map[string]EnvironmentMetricRule, len(c.Metrics))
	for key, value := range c.Metrics {
		if value.Comfort != nil {
			comfort := *value.Comfort
			if comfort.Below != nil {
				bound := *comfort.Below
				comfort.Below = &bound
			}
			if comfort.Above != nil {
				bound := *comfort.Above
				comfort.Above = &bound
			}
			value.Comfort = &comfort
		}
		copy.Metrics[key] = value
	}
	return copy
}
