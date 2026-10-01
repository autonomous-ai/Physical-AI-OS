package mqtthandler

import (
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

// voiceHAL fakes HAL /speaker/* behind http.DefaultTransport.
type voiceHAL struct {
	mu      sync.Mutex
	replies map[string]voiceHALReply // by URL path
	calls   chan voiceHALCall
}

type voiceHALReply struct {
	status int
	body   string
}

type voiceHALCall struct {
	path string
	body map[string]any
}

func (s *voiceHAL) set(path string, status int, body string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.replies[path] = voiceHALReply{status, body}
}

func (s *voiceHAL) RoundTrip(r *http.Request) (*http.Response, error) {
	var body map[string]any
	if r.Body != nil {
		_ = json.NewDecoder(r.Body).Decode(&body)
	}
	s.mu.Lock()
	rep, ok := s.replies[r.URL.Path]
	s.mu.Unlock()
	if !ok {
		rep = voiceHALReply{http.StatusNotFound, `{"detail":"no stub"}`}
	}
	s.calls <- voiceHALCall{r.URL.Path, body}
	return &http.Response{StatusCode: rep.status, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(rep.body))}, nil
}

func (s *voiceHAL) noCall(t *testing.T) {
	t.Helper()
	select {
	case c := <-s.calls:
		t.Fatalf("unexpected HAL call %s %v", c.path, c.body)
	default:
	}
}

// newVoiceTest wires a handler to a fake HAL and a temp users dir holding
// alice/voice/{a.wav,a.npy,b.wav}.
func newVoiceTest(t *testing.T) (*DeviceMQTTHandler, *voiceHAL, string, func(kind, data string), func(t *testing.T, kind, want string) domain.MQTTDataResponse) {
	stub := &voiceHAL{replies: map[string]voiceHALReply{}, calls: make(chan voiceHALCall, 8)}
	original := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = original })
	http.DefaultTransport = stub

	root := t.TempDir()
	dir := filepath.Join(root, "alice", "voice")
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{"a.wav", "a.npy", "b.wav"} {
		if err := os.WriteFile(filepath.Join(dir, f), []byte("RIFF-"+f), 0600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.MkdirAll(filepath.Join(root, "bob"), 0700); err != nil { // face-only person
		t.Fatal(err)
	}
	oldDir := voiceUsersDir
	t.Cleanup(func() { voiceUsersDir = oldDir })
	voiceUsersDir = root

	factory, messages := statusBroker(t)
	h := &DeviceMQTTHandler{config: &config.Config{DeviceID: "voice-test", FDChannel: "test/fd"}, mqttFactory: factory}
	send := func(kind, data string) {
		t.Helper()
		if err := h.dispatchData(domain.MQTTDataCommand{Kind: kind, Data: json.RawMessage(data)}); err != nil {
			t.Fatal(err)
		}
	}
	read := func(t *testing.T, kind, want string) domain.MQTTDataResponse {
		t.Helper()
		select {
		case payload := <-messages:
			var reply domain.MQTTDataResponse
			if err := json.Unmarshal(payload, &reply); err != nil {
				t.Fatal(err)
			}
			if reply.Kind != kind || reply.Status != want {
				t.Fatalf("reply = %s; want %s %q", payload, kind, want)
			}
			return reply
		case <-time.After(3 * time.Second):
			t.Fatalf("missing MQTT %s %s reply", kind, want)
		}
		return domain.MQTTDataResponse{}
	}
	return h, stub, dir, send, read
}

func replyData(t *testing.T, r domain.MQTTDataResponse) string {
	t.Helper()
	b, err := json.Marshal(r.Data)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestVoiceEnrollRecordsFifteenSecondsOnLampMic(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)

	stub.set("/speaker/record-enroll", http.StatusOK,
		`{"status":"ok","meta":{"name":"alice","display_name":"alice","num_samples":1,"embedding_dim":192,"enrollment_sources":["web"]}}`)
	send(domain.KindVoiceEnroll, `{"label":"  Alice "}`)
	var starting domain.MQTTVoiceEnrollStarting
	if err := json.Unmarshal([]byte(replyData(t, read(t, domain.KindVoiceEnroll, "starting"))), &starting); err != nil {
		t.Fatal(err)
	}
	if starting.Label != "alice" || starting.DurationSec != 15 {
		t.Fatalf("starting data = %+v", starting)
	}
	call := <-stub.calls
	if call.path != "/speaker/record-enroll" || call.body["name"] != "alice" || call.body["duration_sec"] != float64(15) {
		t.Fatalf("HAL call = %+v", call)
	}
	if _, ok := call.body["origin"]; ok {
		t.Fatalf("origin must be left to HAL's default like the web: %v", call.body)
	}
	if data := replyData(t, read(t, domain.KindVoiceEnroll, "success")); !strings.Contains(data, `"num_samples":1`) || strings.Contains(data, "embedding_dim") {
		t.Fatalf("success data = %s", data)
	}

	stub.set("/speaker/record-enroll", http.StatusConflict, `{"detail":"Privacy switch is on -- microphone recording is blocked"}`)
	send(domain.KindVoiceEnroll, `{"label":"alice"}`)
	read(t, domain.KindVoiceEnroll, "starting")
	<-stub.calls
	if reply := read(t, domain.KindVoiceEnroll, "failure"); !strings.Contains(reply.Error, "409: Privacy switch is on") {
		t.Fatalf("error = %q", reply.Error)
	}

	for _, bad := range []string{`{"label":"  "}`, `{"label":"` + strings.Repeat("a", voiceLabelMaxLen+1) + `"}`, `{"label":`} {
		send(domain.KindVoiceEnroll, bad)
		read(t, domain.KindVoiceEnroll, "failure")
	}
	stub.noCall(t)
}

func TestVoiceOwnersListsVoiceFolders(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	send(domain.KindVoiceOwners, ``)
	if data := replyData(t, read(t, domain.KindVoiceOwners, "success")); data != `{"persons":[{"label":"alice","voice_samples":["a.npy","a.wav","b.wav"]}]}` {
		t.Fatalf("owners data = %s", data)
	}
	stub.noCall(t)
}

func TestVoiceRemoveDropsProfileViaHAL(t *testing.T) {
	_, stub, _, send, read := newVoiceTest(t)
	stub.set("/speaker/remove", http.StatusOK, `{"status":"ok","name":"alice","removed":true}`)
	send(domain.KindVoiceRemove, `{"label":"Alice"}`)
	read(t, domain.KindVoiceRemove, "starting")
	if call := <-stub.calls; call.path != "/speaker/remove" || call.body["name"] != "alice" {
		t.Fatalf("HAL call = %+v", call)
	}
	read(t, domain.KindVoiceRemove, "success")

	stub.set("/speaker/remove", http.StatusNotFound, `{"detail":"voice profile not found: bob"}`)
	send(domain.KindVoiceRemove, `{"label":"bob"}`)
	read(t, domain.KindVoiceRemove, "starting")
	<-stub.calls
	if reply := read(t, domain.KindVoiceRemove, "failure"); !strings.Contains(reply.Error, "404: voice profile not found") {
		t.Fatalf("error = %q", reply.Error)
	}
}

func TestVoiceFileGetReturnsSample(t *testing.T) {
	_, _, _, send, read := newVoiceTest(t)
	send(domain.KindVoiceFileGet, `{"label":"Alice","file":"a.wav"}`)
	var got domain.MQTTVoiceFileContent
	if err := json.Unmarshal([]byte(replyData(t, read(t, domain.KindVoiceFileGet, "success"))), &got); err != nil {
		t.Fatal(err)
	}
	raw, _ := base64.StdEncoding.DecodeString(got.ContentBase64)
	if got.ContentType != "audio/wav" || string(raw) != "RIFF-a.wav" || got.Size != len(raw) {
		t.Fatalf("got %+v", got)
	}
	for _, bad := range []string{`{"label":"alice","file":"../../etc/passwd"}`, `{"label":"..","file":"a.wav"}`, `{"label":"alice","file":"missing.wav"}`} {
		send(domain.KindVoiceFileGet, bad)
		read(t, domain.KindVoiceFileGet, "failure")
	}
}

func TestVoiceFileRemoveDeletesSampleAndEmbedding(t *testing.T) {
	_, stub, dir, send, read := newVoiceTest(t)
	send(domain.KindVoiceFileRemove, `{"label":"alice","file":"a.npy"}`)
	if reply := read(t, domain.KindVoiceFileRemove, "failure"); reply.Error != "only audio samples can be deleted" {
		t.Fatalf("error = %q", reply.Error)
	}

	send(domain.KindVoiceFileRemove, `{"label":"alice","file":"a.wav"}`)
	if data := replyData(t, read(t, domain.KindVoiceFileRemove, "success")); !strings.Contains(data, `"deleted":"a.wav"`) ||
		!strings.Contains(data, `"remaining":1`) || !strings.Contains(data, `"profile_removed":false`) {
		t.Fatalf("data = %s", data)
	}
	for _, f := range []string{"a.wav", "a.npy"} {
		if _, err := os.Stat(filepath.Join(dir, f)); !os.IsNotExist(err) {
			t.Fatalf("%s not deleted", f)
		}
	}
	stub.noCall(t)

	// Last WAV gone → HAL drops the whole profile, as the web route does.
	stub.set("/speaker/remove", http.StatusOK, `{"status":"ok","name":"alice","removed":true}`)
	send(domain.KindVoiceFileRemove, `{"label":"alice","file":"b.wav"}`)
	if call := <-stub.calls; call.path != "/speaker/remove" || call.body["name"] != "alice" {
		t.Fatalf("HAL call = %+v", call)
	}
	if data := replyData(t, read(t, domain.KindVoiceFileRemove, "success")); !strings.Contains(data, `"profile_removed":true`) {
		t.Fatalf("data = %s", data)
	}
}
