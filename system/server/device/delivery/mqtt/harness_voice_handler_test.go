package mqtthandler

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/harness"
	"go.autonomous.ai/os/system/server/config"
)

func TestHarnessVoiceMQTTSharedStateAndValidation(t *testing.T) {
	factory, messages := statusBroker(t)
	h := &DeviceMQTTHandler{config: &config.Config{DeviceID: "voice-test", FDChannel: "test/fd"}, mqttFactory: factory}
	supported := false
	voice := harness.NewVoiceController(nil, harness.VoiceCallbacks{SupportsMode: func(context.Context) (bool, error) { return supported, nil }})
	call := func(kind, data, wantStatus string) harness.VoiceModeState {
		t.Helper()
		if err := h.dispatchData(domain.MQTTDataCommand{Kind: kind, Data: json.RawMessage(data)}); err != nil {
			t.Fatal(err)
		}
		select {
		case raw := <-messages:
			var reply struct {
				Kind   string                 `json:"kind"`
				Status string                 `json:"status"`
				Error  string                 `json:"error"`
				Data   harness.VoiceModeState `json:"data"`
			}
			if err := json.Unmarshal(raw, &reply); err != nil {
				t.Fatal(err)
			}
			if reply.Kind != kind || reply.Status != wantStatus {
				t.Fatalf("unexpected reply: %s", raw)
			}
			if wantStatus == "failure" && reply.Error == "" {
				t.Fatalf("missing error: %s", raw)
			}
			return reply.Data
		case <-time.After(3 * time.Second):
			t.Fatal("missing MQTT response")
		}
		return harness.VoiceModeState{}
	}
	call(domain.KindHarnessVoiceModeGet, "", "failure")
	call(domain.KindHarnessVoiceModeSet, `{"enabled":true}`, "failure")
	h.SetHarnessVoiceController(voice)
	initial := call(domain.KindHarnessVoiceModeGet, "", "success")
	if initial.Enabled || initial.Generation != voice.State().Generation {
		t.Fatal("default snapshot mismatch")
	}
	call(domain.KindHarnessVoiceModeSet, `{"enabled":true}`, "failure")
	if voice.State().Enabled || voice.State().Supported {
		t.Fatal("unsupported mode enabled")
	}
	call(domain.KindHarnessVoiceModeSet, `{"enabled":false}`, "success")
	supported = true
	on := call(domain.KindHarnessVoiceModeSet, `{"enabled":true}`, "success")
	if !on.Supported || !on.Enabled || !voice.State().Enabled || on.Generation == initial.Generation {
		t.Fatal("MQTT did not change shared controller")
	}
	duplicate := call(domain.KindHarnessVoiceModeSet, `{"enabled":true}`, "success")
	if duplicate.Generation != on.Generation || !duplicate.Enabled {
		t.Fatal("duplicate set changed generation or inverted mode")
	}
	for _, data := range []string{"", `null`, `{}`, `{"enabled":null}`, `{"enabled":"false"}`, `{"enabled":1}`, `[]`, `{"enabled":false,"agentId":"other"}`, `{"enabled":false} {}`, `{"enabled":`} {
		call(domain.KindHarnessVoiceModeSet, data, "failure")
		if voice.State().Generation != on.Generation || !voice.State().Enabled {
			t.Fatalf("invalid request mutated state: %s", data)
		}
	}
	off := call(domain.KindHarnessVoiceModeSet, `{"enabled":false}`, "success")
	if off.Enabled || voice.State().Enabled || off.Generation == on.Generation {
		t.Fatal("explicit false was not applied")
	}
	// A web-side update must be visible on the next MQTT read, without a second flag.
	web, err := voice.SetMode(context.Background(), true)
	if err != nil {
		t.Fatal(err)
	}
	read := call(domain.KindHarnessVoiceModeGet, "", "success")
	if read.Generation != web.Generation || !read.Enabled {
		t.Fatal("MQTT did not read web-side state")
	}
	svc, err := harness.NewService(t.TempDir(), harness.Callbacks{})
	if err != nil {
		t.Fatal(err)
	}
	h.SetHarnessService(svc)
	call(domain.KindHarnessPairRevoke, `{}`, "success")
	if voice.State().Enabled {
		t.Fatal("MQTT unpair left voice mode enabled")
	}
}
