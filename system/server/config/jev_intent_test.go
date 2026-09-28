package config

import (
	"encoding/json"
	"testing"
)

func TestJevIntentDefaultOnAndExplicitDisableRoundTrip(t *testing.T) {
	c := Config{}
	if got := c.JevIntentSettings(); !got.Enabled || got.TimeoutMS != 3000 {
		t.Fatalf("unset config should be on: %+v", got)
	}
	if err := json.Unmarshal([]byte(`{"jev_intent":{"enabled":false,"timeout_ms":200}}`), &c); err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(&c)
	if err != nil {
		t.Fatal(err)
	}
	var restored Config
	if err := json.Unmarshal(raw, &restored); err != nil {
		t.Fatal(err)
	}
	if got := restored.JevIntentSettings(); got.Enabled || got.TimeoutMS != 200 {
		t.Fatalf("settings lost on save/load: %+v", got)
	}
}

func TestJevIntentSettings(t *testing.T) {
	for _, tc := range []struct {
		name                 string
		enabled, local       bool
		timeout, wantTimeout int
	}{
		{"disabled", false, true, 500, 500},
		{"default budget", true, true, 0, 3000},
		{"negative budget", true, true, -1, 3000},
		{"master switch", true, false, 500, 500},
		{"three-second budget", true, true, 3000, 3000},
		{"bounded", true, true, 999999, 3000},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := Config{LocalIntent: &tc.local, JevIntent: &JevIntentConfig{Enabled: &tc.enabled, TimeoutMS: tc.timeout}}
			got := c.JevIntentSettings()
			if got.Enabled != (tc.enabled && tc.local) || got.TimeoutMS != tc.wantTimeout {
				t.Fatalf("got enabled=%v timeout=%d", got.Enabled, got.TimeoutMS)
			}
		})
	}
}

func TestJevUsesConfiguredProxyOnly(t *testing.T) {
	c := Config{LLMBaseURL: "https://proxy.example.test/api/v1/ai/v1/", LLMAPIKey: "device-test-key", JevIntent: &JevIntentConfig{}}
	got := c.JevIntentSettings()
	if got.Endpoint != "https://proxy.example.test/api/v1/ai/v1/jev/decisions" || got.APIKey != c.LLMAPIKey || !got.Enabled {
		t.Fatal("proxy routing/auth or default-on changed")
	}
	c.JevIntent = &JevIntentConfig{TimeoutMS: 200}
	if !c.JevIntentSettings().Enabled {
		t.Fatal("omitted enabled must remain on")
	}
	c.LLMBaseURL = ""
	if c.JevIntentSettings().Endpoint != "" {
		t.Fatal("missing proxy must not fall back to a vendor")
	}
}

func TestJevIntentDefaultRespectsLocalMasterSwitch(t *testing.T) {
	disabled := false
	for _, settings := range []*JevIntentConfig{nil, {}, {TimeoutMS: 200}} {
		c := Config{LocalIntent: &disabled, JevIntent: settings}
		if c.JevIntentSettings().Enabled {
			t.Fatal("local_intent=false must disable the default Jev fallback")
		}
	}
}
