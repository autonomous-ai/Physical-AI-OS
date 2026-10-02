package mqtthandler

import (
	"encoding/json"
	"net/http"
	"reflect"
	"strings"
	"testing"
	"time"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

func volumeReply(t *testing.T, r domain.MQTTDataResponse) domain.MQTTVolumeState {
	t.Helper()
	var v domain.MQTTVolumeState
	if err := json.Unmarshal([]byte(replyData(t, r)), &v); err != nil {
		t.Fatal(err)
	}
	return v
}

func micReply(t *testing.T, r domain.MQTTDataResponse) domain.MQTTMicState {
	t.Helper()
	var m domain.MQTTMicState
	if err := json.Unmarshal([]byte(replyData(t, r)), &m); err != nil {
		t.Fatal(err)
	}
	return m
}

func TestVolumeGetReportsShareOfAllowedRange(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	stub.set("/audio/volume", http.StatusOK, `{"control":"DAC","volume":40,"max_volume":80}`)

	send(domain.KindVolumeGet, `{}`)
	if got := volumeReply(t, read(t, domain.KindVolumeGet, "success")); got != (domain.MQTTVolumeState{Volume: 50, Raw: 40, MaxVolume: 80}) {
		t.Fatalf("state = %+v", got)
	}
}

func TestVolumeGetWithoutCeilingIsRaw(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	stub.set("/audio/volume", http.StatusOK, `{"control":"virtual","volume":73,"max_volume":null}`)

	send(domain.KindVolumeGet, `{}`)
	if got := volumeReply(t, read(t, domain.KindVolumeGet, "success")); got != (domain.MQTTVolumeState{Volume: 73, Raw: 73, MaxVolume: 100}) {
		t.Fatalf("state = %+v", got)
	}
}

func TestVolumeSetMapsShareOntoCeiling(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	// GET and POST share the path; the stub answers both with the applied state.
	stub.set("/audio/volume", http.StatusOK, `{"status":"ok","volume":72,"max_volume":80}`)

	send(domain.KindVolumeSet, `{"volume":90}`)
	if got := volumeReply(t, read(t, domain.KindVolumeSet, "success")); got != (domain.MQTTVolumeState{Volume: 90, Raw: 72, MaxVolume: 80}) {
		t.Fatalf("state = %+v", got)
	}
	if c := halCall(t, stub); c.path != "/audio/volume" || c.body != nil {
		t.Fatalf("first call = %+v, want GET for the ceiling", c)
	}
	if c := halCall(t, stub); !reflect.DeepEqual(c.body, map[string]any{"volume": 72.0}) {
		t.Fatalf("POST body = %v, want 90%% of 80", c.body)
	}
}

func TestVolumeSetRejectsBadInputWithoutCallingHAL(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	for _, data := range []string{`{}`, `{"volume":-1}`, `{"volume":101}`, `{"volume":"loud"}`} {
		send(domain.KindVolumeSet, data)
		read(t, domain.KindVolumeSet, "failure")
	}
	stub.noCall(t)
}

func TestMicGetReportsSwitchState(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	stub.set("/voice/status", http.StatusOK, `{"voice_available":true,"voice_listening":false,"tts_available":true,"tts_speaking":false,"mic_muted":true,"hw_mic_switch_muted":true}`)

	send(domain.KindMicGet, `{}`)
	got := micReply(t, read(t, domain.KindMicGet, "success"))
	if !got.Muted || got.HWSwitchMuted == nil || !*got.HWSwitchMuted || !got.Available {
		t.Fatalf("state = %+v", got)
	}
}

func TestMicGetWithoutHardwareSwitchIsNull(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	stub.set("/voice/status", http.StatusOK, `{"voice_available":true,"voice_listening":true,"tts_available":true,"tts_speaking":false,"mic_muted":false,"hw_mic_switch_muted":null}`)

	send(domain.KindMicGet, `{}`)
	if !strings.Contains(replyData(t, read(t, domain.KindMicGet, "success")), `"hw_switch_muted":null`) {
		t.Fatal("hw_switch_muted must be null on devices without a switch")
	}
}

func TestMicSetMutesAndReportsState(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	stub.set("/voice/mute", http.StatusOK, `{"status":"ok"}`)
	stub.set("/voice/status", http.StatusOK, `{"voice_available":true,"voice_listening":false,"tts_available":true,"tts_speaking":false,"mic_muted":true,"hw_mic_switch_muted":false}`)

	send(domain.KindMicSet, `{"muted":true}`)
	if got := micReply(t, read(t, domain.KindMicSet, "success")); !got.Muted {
		t.Fatalf("state = %+v", got)
	}
	if c := halCall(t, stub); c.path != "/voice/mute" {
		t.Fatalf("HAL call = %+v", c)
	}
}

func TestMicSetUnmuteBlockedByHardwareSwitch(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	stub.set("/voice/unmute", http.StatusConflict, `{"detail":"Hardware mic switch is off -- flip the physical switch to unmute"}`)

	send(domain.KindMicSet, `{"muted":false}`)
	if r := read(t, domain.KindMicSet, "failure"); r.Error != errHWMicSwitch {
		t.Fatalf("error = %q", r.Error)
	}
}

func TestMicSetRequiresMuted(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	send(domain.KindMicSet, `{}`)
	read(t, domain.KindMicSet, "failure")
	stub.noCall(t)
}

func TestRealtimeGetReturnsConfigAndOptionsWithoutKey(t *testing.T) {
	h, _, _, send, read := newVoiceTest(t)
	h.config.Realtime = config.DefaultRealtimeConfig()
	h.config.Realtime.APIKey = "secret-realtime-key"
	h.deviceService = device.ProvideService(h.config, nil, nil, nil, nil)

	send(domain.KindRealtimeGet, `{}`)
	raw := replyData(t, read(t, domain.KindRealtimeGet, "success"))
	if strings.Contains(raw, "secret-realtime-key") {
		t.Fatal("realtime.get leaked the API key")
	}
	var got domain.MQTTRealtimeGetData
	if err := json.Unmarshal([]byte(raw), &got); err != nil {
		t.Fatal(err)
	}
	if !got.Config.Enabled || got.Config.Provider != "gemini" || !got.Config.HasAPIKey || got.Config.Voice == "" {
		t.Fatalf("config = %+v", got.Config)
	}
	if len(got.Options.Providers) == 0 || len(got.Options.Voices["gemini"]) == 0 {
		t.Fatalf("options = %+v", got.Options)
	}
}

func TestRealtimeSetAckNeverEchoesKey(t *testing.T) {
	factory, messages := statusBroker(t)
	h := &DeviceMQTTHandler{config: &config.Config{DeviceID: "rt-test", FDChannel: "test/fd"}, mqttFactory: factory}
	h.publishRealtimeSetAck("failure", "boom", &domain.RealtimeSetData{Provider: "gemini", APIKey: "secret-realtime-key"})
	select {
	case payload := <-messages:
		if strings.Contains(string(payload), "secret-realtime-key") {
			t.Fatalf("ack leaked the key: %s", payload)
		}
		if !strings.Contains(string(payload), `"provider":"gemini"`) {
			t.Fatalf("ack lost the other fields: %s", payload)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("missing realtime.set ack")
	}
}
