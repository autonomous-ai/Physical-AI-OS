package mqtthandler

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

func TestParseFaceEnrollData(t *testing.T) {
	img := base64.StdEncoding.EncodeToString([]byte("jpeg-bytes"))
	req, errMsg := parseFaceEnrollData(json.RawMessage(fmt.Sprintf(
		`{"image_base64":"data:image/jpeg;base64,%s","label":"  Alice ","telegram_username":" alice_tg ","telegram_id":"42"}`, img)))
	if errMsg != "" {
		t.Fatalf("unexpected error: %s", errMsg)
	}
	if req.ImageBase64 != img || req.Label != "Alice" || req.TelegramUsername != "alice_tg" || req.TelegramID != "42" {
		t.Fatalf("req = %+v", req)
	}
}

func TestParseFaceEnrollDataRejectsInvalidInput(t *testing.T) {
	img := base64.StdEncoding.EncodeToString([]byte("jpeg-bytes"))
	for _, payload := range []string{
		`{"image_base64":"` + img + `","label":"  "}`,
		`{"image_base64":"` + img + `","label":"` + strings.Repeat("a", faceEnrollLabelMaxLen+1) + `"}`,
		`{"image_base64":"","label":"alice"}`,
		`{"image_base64":"data:image/jpeg;base64,","label":"alice"}`,
		`{"image_base64":"%%%","label":"alice"}`,
		`{"image_base64":"` + strings.Repeat("a", base64.StdEncoding.EncodedLen(faceEnrollMaxBytes)+4) + `","label":"alice"}`,
		`{"image_base64":`,
	} {
		if _, errMsg := parseFaceEnrollData(json.RawMessage(payload)); errMsg == "" {
			t.Errorf("parseFaceEnrollData(%.80s) succeeded; want failure", payload)
		}
	}
}

type faceEnrollTransport func(*http.Request) (*http.Response, error)

func (f faceEnrollTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestFaceEnrollMQTTForwardsToHAL(t *testing.T) {
	img := base64.StdEncoding.EncodeToString([]byte("jpeg-bytes"))
	halBodies := make(chan map[string]any, 4)
	halStatus, halReply := http.StatusOK, `{"status":"ok","label":"alice","photo_path":"/u/alice/1.jpg","enrolled_count":3}`
	original := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = original })
	http.DefaultTransport = faceEnrollTransport(func(r *http.Request) (*http.Response, error) {
		if r.URL.Path != "/face/enroll" {
			t.Errorf("unexpected HAL path %s", r.URL.Path)
		}
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		// Read the canned reply before signalling so subtests can swap it race-free.
		resp := &http.Response{StatusCode: halStatus, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(halReply))}
		halBodies <- body
		return resp, nil
	})
	factory, messages := statusBroker(t)
	h := &DeviceMQTTHandler{config: &config.Config{DeviceID: "face-enroll-test", FDChannel: "test/fd"}, mqttFactory: factory}

	read := func(t *testing.T, want string) domain.MQTTDataResponse {
		t.Helper()
		select {
		case payload := <-messages:
			var reply domain.MQTTDataResponse
			if err := json.Unmarshal(payload, &reply); err != nil {
				t.Fatal(err)
			}
			if reply.Kind != domain.KindFaceEnroll || reply.Status != want {
				t.Fatalf("reply = %s; want status %q", payload, want)
			}
			return reply
		case <-time.After(3 * time.Second):
			t.Fatalf("missing MQTT %s reply", want)
		}
		return domain.MQTTDataResponse{}
	}
	send := func(data string) {
		if err := h.dispatchData(domain.MQTTDataCommand{Kind: domain.KindFaceEnroll, Data: json.RawMessage(data)}); err != nil {
			t.Fatal(err)
		}
	}

	t.Run("success", func(t *testing.T) {
		send(`{"image_base64":"` + img + `","label":"alice"}`)
		read(t, "starting")
		reply := read(t, "success")
		body := <-halBodies
		if body["label"] != "alice" || body["image_base64"] != img {
			t.Fatalf("HAL body = %v", body)
		}
		data, _ := json.Marshal(reply.Data)
		if !strings.Contains(string(data), `"enrolled_count":3`) || !strings.Contains(string(data), `"photo_path":"/u/alice/1.jpg"`) {
			t.Fatalf("success data = %s", data)
		}
	})

	t.Run("HAL rejects photo", func(t *testing.T) {
		halStatus, halReply = http.StatusBadRequest, `{"detail":"no face detected"}`
		send(`{"image_base64":"` + img + `","label":"alice"}`)
		read(t, "starting")
		<-halBodies
		if reply := read(t, "failure"); !strings.Contains(reply.Error, "no face detected") {
			t.Fatalf("error = %q", reply.Error)
		}
	})

	t.Run("invalid payload never reaches HAL", func(t *testing.T) {
		send(`{"image_base64":"` + img + `"}`)
		read(t, "failure")
		select {
		case b := <-halBodies:
			t.Fatalf("HAL called with %v", b)
		default:
		}
	})
}
