package config

import "fmt"

const (
	VoiceInputAutomatic = "automatic"
	VoiceInputTapToTalk = "tap_to_talk"
)

// GetVoiceInputMode preserves automatic behavior for older configurations.
func (c *Config) GetVoiceInputMode() string {
	if c.VoiceInputMode == VoiceInputTapToTalk {
		return VoiceInputTapToTalk
	}
	return VoiceInputAutomatic
}

// ValidateVoiceInputMode rejects unknown modes before any configuration mutation.
func ValidateVoiceInputMode(mode string) error {
	if mode != VoiceInputAutomatic && mode != VoiceInputTapToTalk {
		return fmt.Errorf("voice_input_mode must be automatic or tap_to_talk")
	}
	return nil
}
