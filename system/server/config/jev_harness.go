package config

import "go.autonomous.ai/os/system/intent/jev"

// JevHarnessSettings configures Harness session selection independently of local intents.
// Selection is enabled by default; uncertainty or provider failure defers to the main agent.
func (c *Config) JevHarnessSettings() JevIntentSettings {
	settings := c.JevIntentSettings()
	settings.Enabled = true
	settings.TimeoutMS = 1500
	if c.JevHarness != nil {
		if c.JevHarness.Enabled != nil {
			settings.Enabled = *c.JevHarness.Enabled
		}
		if c.JevHarness.TimeoutMS > 0 {
			settings.TimeoutMS = c.JevHarness.TimeoutMS
		}
	}
	if settings.TimeoutMS > int(jev.MaxTimeout.Milliseconds()) {
		settings.TimeoutMS = int(jev.MaxTimeout.Milliseconds())
	}
	return settings
}
