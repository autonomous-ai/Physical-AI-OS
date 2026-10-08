package config

import "testing"

func TestGELFRelayCredentials(t *testing.T) {
	const shipped = "https://device-api.autonomous.ai/api/v1/ai/v1"
	const staging = "https://device-api.staging.autonomousdev.xyz/api/v1/ai/v1"
	cases := []struct {
		name     string
		cfg      *Config
		wantBase string
		wantKey  string
	}{
		{
			name: "shipped defaults win over a BYO provider",
			cfg: &Config{
				LLMBaseURL: "https://openrouter.ai/api/v1", LLMAPIKey: "byo-key",
				AutonomousDefaults: &AutonomousDefaults{BaseURL: shipped, APIKey: "shipped-key"},
			},
			wantBase: shipped, wantKey: "shipped-key",
		},
		{
			name:     "untouched device uses its live fields",
			cfg:      &Config{LLMBaseURL: shipped, LLMAPIKey: "live-key"},
			wantBase: shipped, wantKey: "live-key",
		},
		{
			name: "partial defaults fall back to the live fields",
			cfg: &Config{
				LLMBaseURL: shipped, LLMAPIKey: "live-key",
				AutonomousDefaults: &AutonomousDefaults{BaseURL: shipped},
			},
			wantBase: shipped, wantKey: "live-key",
		},
		{
			name:     "unversioned base is normalized",
			cfg:      &Config{LLMBaseURL: "https://device-api.autonomous.ai/api/v1/ai", LLMAPIKey: "k"},
			wantBase: shipped, wantKey: "k",
		},
		{
			name:     "staging host is ours",
			cfg:      &Config{LLMBaseURL: staging, LLMAPIKey: "k"},
			wantBase: staging, wantKey: "k",
		},
		{
			name:     "BYO provider with no shipped defaults gets no relay",
			cfg:      &Config{LLMBaseURL: "https://openrouter.ai/api/v1", LLMAPIKey: "byo-key"},
			wantBase: "", wantKey: "",
		},
		{
			name:     "no credentials at all",
			cfg:      &Config{},
			wantBase: "", wantKey: "",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			base, key := tc.cfg.GELFRelayCredentials()
			if base != tc.wantBase || key != tc.wantKey {
				t.Fatalf("GELFRelayCredentials() = (%q, %q), want (%q, %q)", base, key, tc.wantBase, tc.wantKey)
			}
		})
	}
}

// Refresh tokens for linked accounts must never reach a user-chosen provider.
func TestAutonomousBackend(t *testing.T) {
	const shipped = "https://device-api.autonomous.ai/api/v1/ai/v1"
	const backend = "https://device-api.staging.autonomousdev.xyz/api/v1/ai/v1"
	cases := []struct {
		name     string
		cfg      *Config
		wantBase string
		wantKey  string
		wantOK   bool
	}{
		{
			name: "backend channel wins",
			cfg: &Config{
				BackendBaseURL: backend, BackendAPIKey: "backend-key",
				LLMBaseURL: shipped, LLMAPIKey: "live-key",
			},
			wantBase: backend, wantKey: "backend-key", wantOK: true,
		},
		{
			name: "BYO provider falls back to the shipped defaults",
			cfg: &Config{
				LLMBaseURL: "https://openrouter.ai/api/v1", LLMAPIKey: "byo-key",
				AutonomousDefaults: &AutonomousDefaults{BaseURL: shipped, APIKey: "shipped-key"},
			},
			wantBase: shipped, wantKey: "shipped-key", wantOK: true,
		},
		{
			name:     "untouched device uses its live fields",
			cfg:      &Config{LLMBaseURL: shipped, LLMAPIKey: "live-key"},
			wantBase: shipped, wantKey: "live-key", wantOK: true,
		},
		{
			name:   "BYO provider only is refused",
			cfg:    &Config{LLMBaseURL: "https://api.openai.com/v1", LLMAPIKey: "byo-key"},
			wantOK: false,
		},
		{
			name: "foreign backend channel is skipped",
			cfg: &Config{
				BackendBaseURL: "https://example.com/v1", BackendAPIKey: "x",
				LLMBaseURL: "https://openrouter.ai/api/v1", LLMAPIKey: "byo-key",
			},
			wantOK: false,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			base, key, ok := tc.cfg.AutonomousBackend()
			if base != tc.wantBase || key != tc.wantKey || ok != tc.wantOK {
				t.Fatalf("AutonomousBackend() = (%q, %q, %v), want (%q, %q, %v)",
					base, key, ok, tc.wantBase, tc.wantKey, tc.wantOK)
			}
		})
	}
}
