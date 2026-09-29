package domain

import (
	"encoding/json"
	"testing"

	"go.autonomous.ai/os/system/server/config"
)

func TestNewMQTTInfoResponseIncludesWakeWordState(t *testing.T) {
	enabled := true
	// Pointer: config.Config holds a sync.Mutex that must not be copied.
	tests := []struct {
		name string
		cfg  *config.Config
		want bool
	}{
		{
			name: "enabled",
			cfg:  &config.Config{WakeWord: &enabled},
			want: true,
		},
		{
			name: "missing defaults to disabled",
			cfg:  &config.Config{},
			want: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			msg := NewMQTTInfoResponse(tt.cfg, "info", "00:11:22:33:44:55")
			if msg.WakeWordEnabled != tt.want {
				t.Fatalf("WakeWordEnabled = %t, want %t", msg.WakeWordEnabled, tt.want)
			}

			payload, err := json.Marshal(msg)
			if err != nil {
				t.Fatalf("marshal MQTT info response: %v", err)
			}
			var fields map[string]json.RawMessage
			if err := json.Unmarshal(payload, &fields); err != nil {
				t.Fatalf("unmarshal MQTT info response: %v", err)
			}
			if _, ok := fields["wakeword_enabled"]; !ok {
				t.Fatal("wakeword_enabled must be present even when disabled")
			}
		})
	}
}
