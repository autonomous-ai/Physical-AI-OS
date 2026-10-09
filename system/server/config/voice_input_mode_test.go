package config

import "testing"

func TestVoiceInputModeCompatibility(t *testing.T) {
	for _, value := range []string{"", VoiceInputAutomatic, "unknown"} {
		if got := (&Config{VoiceInputMode: value}).GetVoiceInputMode(); got != VoiceInputAutomatic {
			t.Fatalf("%q resolved to %q", value, got)
		}
	}
	if got := (&Config{VoiceInputMode: VoiceInputTapToTalk}).GetVoiceInputMode(); got != VoiceInputTapToTalk {
		t.Fatal(got)
	}
}
