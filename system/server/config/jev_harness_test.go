package config

import (
	"encoding/json"
	"testing"
)

func TestJevHarnessIndependentDefaultAndDisableRoundtrip(t *testing.T) {
	off := false
	c := Config{LocalIntent: &off, JevIntent: &JevIntentConfig{Enabled: &off}, LLMBaseURL: "https://proxy.test/", LLMAPIKey: "test"}
	got := c.JevHarnessSettings()
	if !got.Enabled || got.TimeoutMS != 1500 || got.Endpoint != "https://proxy.test/jev/decisions" || got.APIKey != "test" {
		t.Fatalf("unexpected default: %+v", got)
	}
	c.JevHarness = &JevIntentConfig{Enabled: &off, TimeoutMS: 9000}
	raw, err := json.Marshal(&c)
	if err != nil {
		t.Fatal(err)
	}
	var restored Config
	if err := json.Unmarshal(raw, &restored); err != nil {
		t.Fatal(err)
	}
	got = restored.JevHarnessSettings()
	if got.Enabled || got.TimeoutMS != 3000 {
		t.Fatalf("flag/budget not retained: %+v", got)
	}
}

func TestJevHarnessConfigurableTimeout(t *testing.T) {
	for _, tc := range []struct{ configured, want int }{{0, 1500}, {-1, 1500}, {500, 500}, {1500, 1500}, {2000, 2000}, {9000, 3000}} {
		c := &Config{JevHarness: &JevIntentConfig{TimeoutMS: tc.configured}}
		if got := c.JevHarnessSettings().TimeoutMS; got != tc.want {
			t.Fatalf("timeout %d resolved to %d, want %d", tc.configured, got, tc.want)
		}
	}
}
