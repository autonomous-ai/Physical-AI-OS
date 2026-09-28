package config

import (
	"go.autonomous.ai/os/system/intent/jev"
	"strings"
)

// JevIntentConfig exposes a kill switch and a bounded classification budget.
// URL/authentication reuse config.json's llm_base_url and llm_api_key.
type JevIntentConfig struct {
	Enabled   *bool `json:"enabled,omitempty" yaml:"enabled"`
	TimeoutMS int   `json:"timeout_ms,omitempty" yaml:"timeoutMs"`
}

// JevIntentSettings is a resolved snapshot of proxy settings and defaults.
type JevIntentSettings struct {
	Enabled   bool
	TimeoutMS int
	Endpoint  string
	APIKey    string
}

// JevIntentSettings resolves config.json without any environment credentials.
// local_intent=false remains the master switch; Jev never replaces local rules
// or runs on its own.
func (c *Config) JevIntentSettings() JevIntentSettings {
	settings := JevIntentSettings{
		Enabled:   true,
		TimeoutMS: int(jev.DefaultTimeout.Milliseconds()),
		APIKey:    strings.TrimSpace(c.LLMAPIKey),
	}
	// Reuse the configured proxy; unavailable routes fall through to the main agent.
	if base := strings.TrimRight(strings.TrimSpace(c.LLMBaseURL), "/"); base != "" {
		settings.Endpoint = base + "/jev/decisions"
	}
	if c.JevIntent != nil {
		settings.TimeoutMS = c.JevIntent.TimeoutMS
		if c.JevIntent.Enabled != nil {
			settings.Enabled = *c.JevIntent.Enabled
		}
	}
	settings.Enabled = settings.Enabled && c.LocalIntentEnabled()
	if settings.TimeoutMS <= 0 {
		settings.TimeoutMS = int(jev.DefaultTimeout.Milliseconds())
	} else if settings.TimeoutMS > int(jev.MaxTimeout.Milliseconds()) {
		settings.TimeoutMS = int(jev.MaxTimeout.Milliseconds())
	}
	return settings
}
