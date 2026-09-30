package bootstrap

import (
	"os"
	"path/filepath"
	"testing"

	"go.autonomous.ai/os/system/lib/urlnorm"
)

func TestLogRelayTargetUsesTheDeviceAutonomousCredential(t *testing.T) {
	// Placeholder base URL under a domain the relay accepts.
	base := "https://api.example." + urlnorm.AutonomousDomains()[0] + "/v1"
	cases := []struct {
		name, json, wantID, wantBase, wantKey string
	}{
		{
			name:     "set-up device",
			json:     `{"device_id":"dev-1","llm_base_url":"` + base + `","llm_api_key":"lob_key"}`,
			wantID:   "dev-1",
			wantBase: base,
			wantKey:  "lob_key",
		},
		{
			name:   "fresh device before setup",
			json:   `{}`,
			wantID: "",
		},
		{
			name:   "owner's own provider never receives device logs",
			json:   `{"device_id":"dev-2","llm_base_url":"https://api.openai.com/v1","llm_api_key":"sk-owner"}`,
			wantID: "dev-2",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "config.json")
			if err := os.WriteFile(path, []byte(tc.json), 0o644); err != nil {
				t.Fatal(err)
			}
			id, base, key, err := logRelayTarget(path)
			if err != nil {
				t.Fatalf("logRelayTarget: %v", err)
			}
			if id != tc.wantID || base != tc.wantBase || key != tc.wantKey {
				t.Errorf("got (%q, %q, %q), want (%q, %q, %q)", id, base, key, tc.wantID, tc.wantBase, tc.wantKey)
			}
		})
	}
}

func TestLogRelayTargetWithoutConfigIsAnError(t *testing.T) {
	if _, _, _, err := logRelayTarget(filepath.Join(t.TempDir(), "missing.json")); err == nil {
		t.Fatal("missing config.json must be reported so the caller keeps spooling")
	}
}
