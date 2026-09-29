package mqtthandler

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"go.autonomous.ai/os/system/device"
	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/network"
	"go.autonomous.ai/os/system/schedule"
	"go.autonomous.ai/os/system/server/config"
)

// Golden vectors from the shared digest spec (see schedule.Digest).
const (
	digestV0 = "v1:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
	digestV1 = "v1:74f9d2edc313294a4b32aa703b46e70cbefae777b977a5b19b254c2ba9044a70"
)

// infoGateway satisfies the one AgentGateway method handleInfo calls.
type infoGateway struct{ domain.AgentGateway }

func (infoGateway) ListSkills() ([]domain.InstalledSkill, error) {
	return nil, errors.New("not needed by this test")
}

// infoTestHandler wires handleInfo minimally and publishes through the fake broker.
func infoTestHandler(t *testing.T, storePath string) (*DeviceMQTTHandler, <-chan []byte) {
	t.Helper()
	t.Chdir(t.TempDir())
	factory, messages := statusBroker(t)
	cfg := &config.Config{DeviceID: "info-test", FDChannel: "test/fd", AgentRuntime: "openclaw"}
	gw := infoGateway{}
	h := &DeviceMQTTHandler{
		config:         cfg,
		mqttFactory:    factory,
		agentGateway:   gw,
		networkService: &network.Service{},
		deviceService:  device.ProvideService(cfg, nil, gw, nil, nil),
		scheduleStore:  schedule.NewStore(storePath),
	}
	return h, messages
}

func publishInfo(t *testing.T, h *DeviceMQTTHandler, messages <-chan []byte) map[string]json.RawMessage {
	t.Helper()
	if err := h.handleInfo(domain.MQTTMessage{Cmd: domain.CommandInfo}); err != nil {
		t.Fatalf("handleInfo: %v", err)
	}
	msg := nextPublish(t, messages)
	if string(msg["type"]) != `"info"` {
		t.Fatalf("published type = %s, want info", msg["type"])
	}
	return msg
}

// The info uplink carries the digest of the schedule store's rows.
func TestHandleInfo_ReportsTheStoresSchedulesDigest(t *testing.T) {
	h, messages := infoTestHandler(t, filepath.Join(t.TempDir(), "schedules.json"))
	if err := h.scheduleStore.Replace([]schedule.Schedule{
		{ID: "b2", Name: "Stretch", Enabled: false},
		{ID: "a1", Name: "Inbox digest", Enabled: true, Rev: 3, Requires: []string{"gmail"}},
	}); err != nil {
		t.Fatalf("seed: %v", err)
	}

	msg := publishInfo(t, h, messages)
	if got := string(msg["schedules_digest"]); got != `"`+digestV1+`"` {
		t.Fatalf("schedules_digest = %s, want %q", got, digestV1)
	}
}

// A missing schedules.json reports the EMPTY digest, not nothing.
func TestHandleInfo_EmptyStoreReportsV0(t *testing.T) {
	h, messages := infoTestHandler(t, filepath.Join(t.TempDir(), "schedules.json"))
	msg := publishInfo(t, h, messages)
	if got := string(msg["schedules_digest"]); got != `"`+digestV0+`"` {
		t.Fatalf("schedules_digest = %s, want %q", got, digestV0)
	}
}

// An unreadable store omits the digest but still sends the uplink.
func TestHandleInfo_UnreadableStoreOmitsDigest(t *testing.T) {
	path := filepath.Join(t.TempDir(), "schedules.json")
	if err := os.Mkdir(path, 0o700); err != nil { // a directory: every read fails
		t.Fatal(err)
	}
	h, messages := infoTestHandler(t, path)

	msg := publishInfo(t, h, messages)
	if v, ok := msg["schedules_digest"]; ok {
		t.Fatalf("schedules_digest = %s, want the key omitted for an unreadable store", v)
	}
	if string(msg["id"]) != `"info-test"` {
		t.Fatalf("info uplink incomplete: %v", msg)
	}
}

// omitempty keeps the digest out of every other reply.
func TestMQTTInfoResponse_SchedulesDigestIsOmitEmpty(t *testing.T) {
	raw, err := json.Marshal(domain.MQTTInfoResponse{ID: "d"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), "schedules_digest") {
		t.Fatalf("JSON = %s, want no schedules_digest key when empty", raw)
	}
	raw, _ = json.Marshal(domain.MQTTInfoResponse{ID: "d", SchedulesDigest: digestV0})
	if !strings.Contains(string(raw), `"schedules_digest":"`+digestV0+`"`) {
		t.Fatalf("JSON = %s, want the schedules_digest key", raw)
	}
}
