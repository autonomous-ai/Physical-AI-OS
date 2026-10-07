package domain

import (
	"encoding/json"
	"go.autonomous.ai/os/system/server/config"
	"testing"
)

func TestMQTTInfoReportsVoiceInputMode(t *testing.T) {
	for _, mode := range []string{"", "automatic", "tap_to_talk"} {
		cfg := &config.Config{VoiceInputMode: mode}
		msg := NewMQTTInfoResponse(cfg, "info", "test")
		data, err := json.Marshal(msg)
		if err != nil {
			t.Fatal(err)
		}
		var fields map[string]any
		if err := json.Unmarshal(data, &fields); err != nil {
			t.Fatal(err)
		}
		if fields["voice_input_mode"] != cfg.GetVoiceInputMode() {
			t.Fatalf("mode=%v", fields["voice_input_mode"])
		}
	}
}
