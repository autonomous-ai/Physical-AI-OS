package hal

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// faceEnrollTimeout covers face detection + embedding training on-device.
const faceEnrollTimeout = 30 * time.Second

// faceClient is swapped in tests; nil uses a client with faceEnrollTimeout.
var faceClient *http.Client

// FaceEnrollRequest mirrors HAL's POST /face/enroll body.
type FaceEnrollRequest struct {
	ImageBase64      string `json:"image_base64"`
	Label            string `json:"label"`
	TelegramUsername string `json:"telegram_username,omitempty"`
	TelegramID       string `json:"telegram_id,omitempty"`
}

// FaceEnrollResult mirrors HAL's FaceEnrollResponse.
type FaceEnrollResult struct {
	Status           string `json:"status"`
	Label            string `json:"label"`
	TelegramUsername string `json:"telegram_username,omitempty"`
	TelegramID       string `json:"telegram_id,omitempty"`
	PhotoPath        string `json:"photo_path"`
	EnrolledCount    int    `json:"enrolled_count"`
}

// FaceEnroll saves one photo under users/{label}/ and trains its embeddings.
// A non-2xx reply returns HAL's `detail` (e.g. "no face detected") as the error.
func FaceEnroll(r FaceEnrollRequest) (*FaceEnrollResult, error) {
	body, err := json.Marshal(r)
	if err != nil {
		return nil, fmt.Errorf("marshal face enroll: %w", err)
	}
	req, err := newRequest("POST", "/face/enroll", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("POST /face/enroll: %w", err)
	}
	client := faceClient
	if client == nil {
		client = &http.Client{Timeout: faceEnrollTimeout}
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("POST /face/enroll: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		var e struct {
			Detail string `json:"detail"`
		}
		_ = json.NewDecoder(io.LimitReader(resp.Body, 4096)).Decode(&e)
		if e.Detail != "" {
			return nil, fmt.Errorf("POST /face/enroll returned %d: %s", resp.StatusCode, e.Detail)
		}
		return nil, fmt.Errorf("POST /face/enroll returned %d", resp.StatusCode)
	}
	var out FaceEnrollResult
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("decode /face/enroll: %w", err)
	}
	return &out, nil
}
