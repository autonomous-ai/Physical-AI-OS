package hal

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// voiceEmbedTimeout covers the ALSA hand-off plus the speaker-embedding call
// that follows the recording.
const voiceEmbedTimeout = 45 * time.Second

// voiceRemoveTimeout covers POST /speaker/remove (disk only).
const voiceRemoveTimeout = 10 * time.Second

// voiceClient is swapped in tests; nil builds a client per call with the right timeout.
var voiceClient *http.Client

// VoiceRecordEnrollRequest mirrors HAL's POST /speaker/record-enroll body;
// origin is left to HAL's default ("web"), as the web Voice settings do.
type VoiceRecordEnrollRequest struct {
	Name        string `json:"name"`
	DurationSec int    `json:"duration_sec"`
}

// VoiceProfile is one person's voice profile, trimmed to identity fields
// (HAL SpeakerMeta without file lists or embedding bookkeeping).
type VoiceProfile struct {
	Name              string   `json:"name"`
	DisplayName       string   `json:"display_name"`
	TelegramUsername  string   `json:"telegram_username,omitempty"`
	TelegramID        string   `json:"telegram_id,omitempty"`
	EnrollmentSources []string `json:"enrollment_sources"`
	NumSamples        int      `json:"num_samples"`
	NumExtended       int      `json:"num_extended"`
	EnrolledAt        string   `json:"enrolled_at,omitempty"`
	UpdatedAt         string   `json:"updated_at,omitempty"`
}

// VoiceEnrollResult mirrors HAL's EnrollResponse.
type VoiceEnrollResult struct {
	Status string       `json:"status"`
	Meta   VoiceProfile `json:"meta"`
}

// VoiceRemoveResult mirrors HAL's RemoveResponse.
type VoiceRemoveResult struct {
	Status  string `json:"status"`
	Name    string `json:"name"`
	Removed bool   `json:"removed"`
}

// VoiceRecordEnroll records r.DurationSec seconds from the lamp's own mic and
// enrolls it under r.Name. Blocks for the whole recording; HAL answers 409 while
// the privacy switch is on and 503 in the simulator or when embedding is down.
func VoiceRecordEnroll(r VoiceRecordEnrollRequest) (*VoiceEnrollResult, error) {
	var out VoiceEnrollResult
	timeout := time.Duration(r.DurationSec)*time.Second + voiceEmbedTimeout
	if err := voiceDo(voiceHTTPClient(timeout), "POST", "/speaker/record-enroll", r, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// VoiceRemove deletes one person's voice profile only; face photos and metadata
// stay. HAL answers 404 when the person has no voice profile.
func VoiceRemove(name string) (*VoiceRemoveResult, error) {
	var out VoiceRemoveResult
	if err := voiceDo(voiceHTTPClient(voiceRemoveTimeout), "POST", "/speaker/remove", map[string]string{"name": name}, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

func voiceHTTPClient(timeout time.Duration) *http.Client {
	if voiceClient != nil {
		return voiceClient
	}
	return &http.Client{Timeout: timeout}
}

// voiceDo sends one /speaker/* request and decodes the JSON reply into out.
// A non-2xx reply returns HAL's `detail` as the error.
func voiceDo(client *http.Client, method, path string, in, out any) error {
	b, err := json.Marshal(in)
	if err != nil {
		return fmt.Errorf("marshal %s: %w", path, err)
	}
	req, err := newRequest(method, path, bytes.NewReader(b))
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		var e struct {
			Detail string `json:"detail"`
		}
		_ = json.NewDecoder(io.LimitReader(resp.Body, 4096)).Decode(&e)
		if e.Detail != "" {
			return fmt.Errorf("%s %s returned %d: %s", method, path, resp.StatusCode, e.Detail)
		}
		return fmt.Errorf("%s %s returned %d", method, path, resp.StatusCode)
	}
	if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
		return fmt.Errorf("decode %s: %w", path, err)
	}
	return nil
}
