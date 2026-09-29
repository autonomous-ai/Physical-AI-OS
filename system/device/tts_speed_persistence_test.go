package device

import (
	"encoding/json"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/server/config"
)

type ttsPersistenceTransport func(*http.Request) (*http.Response, error)

func (f ttsPersistenceTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestTTSSpeedPersistsAndAppliesAcrossHTTPAndMQTT(t *testing.T) {
	t.Chdir(t.TempDir())
	t.Setenv("HAL_TTS_SPEED", "1.1")
	original := http.DefaultTransport
	t.Cleanup(func() { http.DefaultTransport = original })
	type appliedConfig struct {
		path string
		body map[string]any
		err  error
	}
	applied := make(chan appliedConfig, 5)
	http.DefaultTransport = ttsPersistenceTransport(func(r *http.Request) (*http.Response, error) {
		var body map[string]any
		err := json.NewDecoder(r.Body).Decode(&body)
		applied <- appliedConfig{r.URL.Path, body, err}
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header), Body: io.NopCloser(strings.NewReader(`{}`))}, nil
	})
	cfg := baseConfig()
	cfg.TTSProvider = "elevenlabs"
	if err := cfg.Save(); err != nil {
		t.Fatal(err)
	}

	for _, tc := range []struct {
		name  string
		mqtt  bool
		speed *float64
		voice string
		want  float64
	}{
		{name: "MQTT persists explicit rate", mqtt: true, speed: speedPointer(1.2), want: 1.2},
		{name: "HTTP voice change preserves rate", voice: "voice-http", want: 1.2},
		{name: "HTTP restores normal rate despite legacy env", speed: speedPointer(1.0), want: 1.0},
		{name: "MQTT voice change preserves normal rate", mqtt: true, voice: "voice-mqtt", want: 1.0},
		{name: "MQTT replay reapplies unchanged rate", mqtt: true, voice: "voice-mqtt", speed: speedPointer(1.0), want: 1.0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// Reload to prove disk persistence and isolate each async HAL apply.
			loaded, err := config.Load()
			if err != nil {
				t.Fatal(err)
			}
			// Clear the marker so an unchanged replay waits for its own HAL apply.
			if err := os.Remove("config/.hal_config_hash"); err != nil && !os.IsNotExist(err) {
				t.Fatal(err)
			}
			service := &Service{config: loaded}
			if tc.mqtt {
				err = service.UpdateVoiceConfig("", tc.voice, "", tc.speed)
			} else {
				err = service.UpdateConfig(domain.UpdateConfigRequest{TTSSpeed: tc.speed, TTSVoice: tc.voice})
			}
			if err != nil {
				t.Fatal(err)
			}
			reloaded, err := config.Load()
			if err != nil {
				t.Fatal(err)
			}
			if reloaded.TTSSpeed == nil || reloaded.GetTTSSpeed() != tc.want {
				t.Fatalf("reloaded speed = %v, want %v", reloaded.TTSSpeed, tc.want)
			}
			raw, err := os.ReadFile("config/config.json")
			if err != nil {
				t.Fatal(err)
			}
			var persisted map[string]any
			if err := json.Unmarshal(raw, &persisted); err != nil {
				t.Fatal(err)
			}
			if persisted["tts_speed"] != tc.want {
				t.Fatalf("persisted speed = %v", persisted["tts_speed"])
			}
			select {
			case got := <-applied:
				if got.err != nil {
					t.Fatal(got.err)
				}
				if got.path != "/voice/tts/config" || got.body["speed"] != tc.want || got.body["provider"] != "elevenlabs" {
					t.Fatalf("HAL request = %+v", got)
				}
				if tc.voice != "" && (got.body["voice"] != tc.voice || reloaded.TTSVoice != tc.voice) {
					t.Fatalf("voice update lost: %+v", got)
				}
			case <-time.After(3 * time.Second):
				t.Fatal("HAL live update not sent")
			}
			// Wait for the boot snapshot write before changing cwd or the next update.
			deadline := time.Now().Add(3 * time.Second)
			for config.HALConfigChanged() {
				if time.Now().After(deadline) {
					t.Fatal("HAL apply did not finish its config snapshot")
				}
				time.Sleep(time.Millisecond)
			}
		})
	}
}

func speedPointer(speed float64) *float64 { return &speed }
