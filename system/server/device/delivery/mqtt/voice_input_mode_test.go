package mqtthandler

import (
	"encoding/json"
	"path/filepath"
	"testing"

	"go.autonomous.ai/os/system/domain"
)

func TestVoiceInputModeRejectsInvalidBeforeStarting(t *testing.T) {
	for _, payload := range []string{`{}`, `null`, `{"mode":""}`, `{"mode":"manual"}`, `{"mode":true}`} {
		t.Run(payload, func(t *testing.T) {
			h, messages := infoTestHandler(t, filepath.Join(t.TempDir(), "schedules.json"))
			if err := h.handleVoiceInputMode(domain.MQTTDataCommand{Data: json.RawMessage(payload)}); err == nil {
				t.Fatal("invalid payload accepted")
			}
			msg := nextPublish(t, messages)
			if string(msg["status"]) != `"failure"` || string(msg["kind"]) != `"voice.input_mode"` {
				t.Fatalf("ack = %v", msg)
			}
			if h.config.VoiceInputMode != "" {
				t.Fatal("invalid payload mutated mode")
			}
		})
	}
}

func TestVoiceInputModeAcknowledgesUnchangedDefault(t *testing.T) {
	h, messages := infoTestHandler(t, filepath.Join(t.TempDir(), "schedules.json"))
	if err := h.handleVoiceInputMode(domain.MQTTDataCommand{Data: json.RawMessage(`{"mode":"automatic"}`)}); err != nil {
		t.Fatal(err)
	}
	for _, status := range []string{`"starting"`, `"success"`} {
		msg := nextPublish(t, messages)
		if string(msg["status"]) != status || string(msg["voice_input_mode"]) != `"automatic"` {
			t.Fatalf("ack = %v", msg)
		}
	}
}
