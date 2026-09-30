package hal

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// faceTimeout covers face detection + embedding (re)training on-device.
const faceTimeout = 30 * time.Second

// faceClient is swapped in tests; nil uses a client with faceTimeout.
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

// FaceOwner is one person from HAL's GET /face/owners, trimmed to identity fields.
type FaceOwner struct {
	Label            string   `json:"label"`
	TelegramUsername string   `json:"telegram_username,omitempty"`
	TelegramID       string   `json:"telegram_id,omitempty"`
	PhotoCount       int      `json:"photo_count"`
	Photos           []string `json:"photos"`
}

// FaceOwners mirrors HAL's FaceOwnersDetailResponse (identity fields only).
type FaceOwners struct {
	EnrolledCount int         `json:"enrolled_count"`
	Persons       []FaceOwner `json:"persons"`
}

// FaceRemoveResult mirrors HAL's FaceRemoveResponse.
type FaceRemoveResult struct {
	Status        string `json:"status"`
	Label         string `json:"label"`
	EnrolledCount int    `json:"enrolled_count"`
}

// FaceEnroll saves one photo under users/{label}/ and trains its embeddings.
// A non-2xx reply returns HAL's `detail` (e.g. "no face detected") as the error.
func FaceEnroll(r FaceEnrollRequest) (*FaceEnrollResult, error) {
	var out FaceEnrollResult
	if err := faceDo("POST", "/face/enroll", r, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// GetFaceOwners lists people with a face photo, voice sample or metadata.json
// (plus HAL's shared "unknown" bucket, if present).
func GetFaceOwners() (*FaceOwners, error) {
	var out FaceOwners
	if err := faceDo("GET", "/face/owners", nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// FaceRemove deletes one person's whole users/{label}/ folder (face, voice, metadata,
// history) and retrains the rest; HAL answers 404 for an unknown label.
func FaceRemove(label string) (*FaceRemoveResult, error) {
	var out FaceRemoveResult
	if err := faceDo("POST", "/face/remove", map[string]string{"label": label}, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// faceDo sends one /face/* request and decodes the JSON reply into out.
// A non-2xx reply returns HAL's `detail` as the error.
func faceDo(method, path string, in, out any) error {
	var body io.Reader
	if in != nil {
		b, err := json.Marshal(in)
		if err != nil {
			return fmt.Errorf("marshal %s: %w", path, err)
		}
		body = bytes.NewReader(b)
	}
	req, err := newRequest(method, path, body)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	client := faceClient
	if client == nil {
		client = &http.Client{Timeout: faceTimeout}
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
