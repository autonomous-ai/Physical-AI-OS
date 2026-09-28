package config

import (
	"encoding/json"
	"math"
	"reflect"
	"testing"
)

func TestEnvironmentDefaultsAndPartialConfig(t *testing.T) {
	var c Config
	if err := json.Unmarshal([]byte(`{"environment":{"cooldown_s":0,"metrics":{"voc_index":{"delta":25}}}}`), &c); err != nil {
		t.Fatal(err)
	}
	settings := c.EnvironmentSettings()
	if settings.CooldownS != 0 || settings.EvaluateIntervalS != 10 || len(settings.Metrics) != 1 || settings.Metrics["voc_index"].WarmupS != 3600 {
		t.Fatalf("wrong effective config: %+v", settings)
	}
	settings.Metrics["voc_index"] = EnvironmentMetricRule{Delta: 999}
	if c.EnvironmentSettings().Metrics["voc_index"].Delta != 25 {
		t.Fatal("settings returned shared map")
	}
	var old Config
	if got := old.EnvironmentSettings(); !got.Enabled || !got.InitialReport || got.SustainS != 60 || got.CooldownS != 1800 || len(got.Metrics) != 9 {
		t.Fatalf("old config defaults: %+v", got)
	}
	if Default().Environment == nil {
		t.Fatal("new config must persist editable defaults")
	}
	for _, tc := range []struct {
		body string
		want float64
	}{
		{`{}`, 1800},
		{`{"cooldown_s":900}`, 900},
		{`{"cooldown_s":0}`, 0},
	} {
		var settings EnvironmentConfig
		if err := json.Unmarshal([]byte(tc.body), &settings); err != nil {
			t.Fatal(err)
		}
		if settings.CooldownS != tc.want {
			t.Fatalf("%s: cooldown = %v, want %v", tc.body, settings.CooldownS, tc.want)
		}
	}
}

func TestEnvironmentRejectsInvalidConfig(t *testing.T) {
	for _, body := range []string{
		`{"evaluate_interval_s":0}`, `{"sustain_s":1}`, `{"retry_interval_s":1}`,
		`{"cooldown_s":-1}`, `{"max_sample_age_s":0}`, `{"enabled":null}`, `{"initial_report":null}`, `{"initial_report":"false"}`,
		`{"metrics":{}}`, `{"metrics":null}`, `{"unknown":1}`,
		`{"metrics":{"co_ppm":{"delta":10}}}`, `{"metrics":{"voc_index":null}}`,
		`{"metrics":{"voc_index":{"delta":0}}}`, `{"metrics":{"voc_index":{"warmup_s":null}}}`,
		`{"metrics":{"voc_index":{"warmup_s":-1}}}`, `{"metrics":{"voc_index":{"typo":2}}}`,
		`{"metrics":{"co2_ppm":{"relative_delta_pct":-1}}}`,
		`{"metrics":{"co2_ppm":{"relative_delta_pct":101}}}`,
		`{"metrics":{"co2_ppm":{"relative_delta_pct":null}}}`,
		`{"metrics":{"co2_ppm":{"relative_delta_pct":"20"}}}`,
	} {
		t.Run(body, func(t *testing.T) {
			var c EnvironmentConfig
			if err := json.Unmarshal([]byte(body), &c); err == nil {
				t.Fatal("invalid config accepted")
			}
		})
	}
	c := DefaultEnvironmentConfig()
	c.EvaluateIntervalS = math.NaN()
	if c.Validate() == nil {
		t.Fatal("NaN accepted")
	}
}

func TestEnvironmentCO2DefaultsAndExplicitSubset(t *testing.T) {
	var c EnvironmentConfig
	if err := json.Unmarshal([]byte(`{}`), &c); err != nil {
		t.Fatal(err)
	}
	rule := c.Metrics["co2_ppm"]
	if rule.Delta != 200 || rule.RelativeDeltaPct != 20 || rule.WarmupS != 60 || rule.Comfort == nil || *rule.Comfort.Above != 1000 {
		t.Fatal(c.Metrics)
	}
	if err := json.Unmarshal([]byte(`{"metrics":{"temperature_c":{"delta":3}}}`), &c); err != nil {
		t.Fatal(err)
	}
	if _, ok := c.Metrics["co2_ppm"]; ok {
		t.Fatal("explicit subset unexpectedly monitors CO2")
	}
	if err := json.Unmarshal([]byte(`{"metrics":{"co2_ppm":{"delta":300}}}`), &c); err != nil {
		t.Fatal(err)
	}
	if c.Metrics["co2_ppm"].WarmupS != 60 {
		t.Fatal(c.Metrics)
	}
}

func TestEnvironmentInitialReportCanBeDisabledIndependently(t *testing.T) {
	var c EnvironmentConfig
	if err := json.Unmarshal([]byte(`{"initial_report":false}`), &c); err != nil {
		t.Fatal(err)
	}
	if c.InitialReport || !c.Enabled || len(c.Metrics) != 9 {
		t.Fatalf("initial report toggle changed sensing policy: %+v", c)
	}
}

func TestEnvironmentRelativeDeltaCompatibility(t *testing.T) {
	for _, tc := range []struct {
		name            string
		body            string
		delta, relative float64
	}{
		{"omitted metrics gets new defaults", `{}`, 200, 20},
		{"legacy explicit rule stays absolute", `{"metrics":{"co2_ppm":{"delta":300,"warmup_s":60}}}`, 300, 0},
		{"partial explicit rule stays absolute", `{"metrics":{"co2_ppm":{"warmup_s":90}}}`, 200, 0},
		{"explicit zero disables percentage", `{"metrics":{"co2_ppm":{"relative_delta_pct":0}}}`, 200, 0},
		{"opt in to percentage", `{"metrics":{"co2_ppm":{"relative_delta_pct":20}}}`, 200, 20},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var cfg EnvironmentConfig
			if err := json.Unmarshal([]byte(tc.body), &cfg); err != nil {
				t.Fatal(err)
			}
			rule := cfg.Metrics["co2_ppm"]
			if rule.Delta != tc.delta || rule.RelativeDeltaPct != tc.relative {
				t.Fatalf("unexpected rule: %+v", rule)
			}
			body, err := json.Marshal(cfg)
			if err != nil {
				t.Fatal(err)
			}
			var restored EnvironmentConfig
			if err := json.Unmarshal(body, &restored); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(restored.Metrics["co2_ppm"], rule) {
				t.Fatal("round trip changed rule")
			}
		})
	}
	for _, value := range []float64{math.NaN(), math.Inf(1), math.Inf(-1)} {
		cfg := DefaultEnvironmentConfig()
		rule := cfg.Metrics["co2_ppm"]
		rule.RelativeDeltaPct = value
		cfg.Metrics["co2_ppm"] = rule
		if cfg.Validate() == nil {
			t.Fatalf("accepted relative percentage %v", value)
		}
	}
}

func TestEnvironmentComfortCompatibilityAndIsolation(t *testing.T) {
	var legacy EnvironmentConfig
	if err := json.Unmarshal([]byte(`{"metrics":{"co2_ppm":{"delta":200}}}`), &legacy); err != nil {
		t.Fatal(err)
	}
	if legacy.Metrics["co2_ppm"].Comfort != nil {
		t.Fatal("legacy rule gained unsolicited comfort events")
	}
	body := `{"metrics":{"co2_ppm":{"comfort":{"above":1500,"hysteresis":200,"sustain_s":300}}}}`
	var cfg EnvironmentConfig
	if err := json.Unmarshal([]byte(body), &cfg); err != nil {
		t.Fatal(err)
	}
	rule := cfg.Metrics["co2_ppm"]
	if rule.Comfort == nil || *rule.Comfort.Above != 1500 || rule.RelativeDeltaPct != 0 {
		t.Fatalf("bad opt-in: %+v", rule)
	}
	original := DefaultEnvironmentConfig()
	clone := original.Clone()
	*clone.Metrics["temperature_c"].Comfort.Below = 5
	*clone.Metrics["temperature_c"].Comfort.Above = 99
	clone.Metrics["temperature_c"].Comfort.Hysteresis = 50
	rule = original.Metrics["temperature_c"]
	if *rule.Comfort.Below != 19 || *rule.Comfort.Above != 27 || rule.Comfort.Hysteresis != 1 {
		t.Fatal("clone shares comfort settings")
	}
}

func TestEnvironmentComfortRejectsInvalidConfig(t *testing.T) {
	for _, comfort := range []string{
		`null`, `{}`,
		`{"above":1000,"hysteresis":0,"sustain_s":300}`,
		`{"above":1000,"hysteresis":-1,"sustain_s":300}`,
		`{"above":1000,"hysteresis":100,"sustain_s":9}`,
		`{"above":1000,"hysteresis":100,"sustain_s":86401}`,
		`{"below":27,"above":19,"hysteresis":1,"sustain_s":300}`,
		`{"below":19,"above":27,"hysteresis":5,"sustain_s":300}`,
		`{"above":null,"hysteresis":1,"sustain_s":300}`,
		`{"above":1000,"hysteresis":1,"sustain_s":null}`,
		`{"above":1000,"hysteresis":1,"sustain_s":300,"typo":1}`,
	} {
		var cfg EnvironmentConfig
		if err := json.Unmarshal([]byte(`{"metrics":{"co2_ppm":{"comfort":`+comfort+`}}}`), &cfg); err == nil {
			t.Fatalf("accepted %s", comfort)
		}
	}
	for _, field := range []string{"above", "below", "hysteresis", "sustain"} {
		for _, value := range []float64{math.NaN(), math.Inf(1), math.Inf(-1)} {
			cfg := DefaultEnvironmentConfig()
			r := cfg.Metrics["temperature_c"].Comfort
			switch field {
			case "above":
				*r.Above = value
			case "below":
				*r.Below = value
			case "hysteresis":
				r.Hysteresis = value
			case "sustain":
				r.SustainS = value
			}
			if cfg.Validate() == nil {
				t.Fatalf("accepted nonfinite %s", field)
			}
		}
	}
}
