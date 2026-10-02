package hal

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
)

// RestingLEDLook is one resting LED look as HAL reports it.
type RestingLEDLook struct {
	Effect string   `json:"effect"`
	Color  []int    `json:"color"`
	Speed  *float64 `json:"speed,omitempty"`
}

// RestingLED is HAL GET/PUT /led/resting: the owner's choice, the device
// default from presets.json and the look the strip settles on.
type RestingLED struct {
	Mode      string         `json:"mode"`
	Color     []int          `json:"color"`
	Default   RestingLEDLook `json:"default"`
	Effective RestingLEDLook `json:"effective"`
}

// RestingLEDChoice is the body of HAL PUT /led/resting.
type RestingLEDChoice struct {
	Mode  string `json:"mode"`
	Color []int  `json:"color,omitempty"`
}

// RestingLEDPreview is HAL POST /led/resting/preview's reply; Painted is false
// while sleep, speech or music owns the strip.
type RestingLEDPreview struct {
	Status  string `json:"status"`
	Painted bool   `json:"painted"`
}

// GetRestingLED reads the owner's resting LED choice.
func GetRestingLED() (*RestingLED, error) {
	var out RestingLED
	if err := ledDo(http.MethodGet, "/led/resting", nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// SetRestingLED saves the owner's resting LED choice and shows it when resting.
func SetRestingLED(choice RestingLEDChoice) (*RestingLED, error) {
	var out RestingLED
	if err := ledDo(http.MethodPut, "/led/resting", choice, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// PreviewRestingLED paints a candidate resting colour without saving it.
func PreviewRestingLED(color []int) (*RestingLEDPreview, error) {
	var out RestingLEDPreview
	if err := ledDo(http.MethodPost, "/led/resting/preview", map[string][]int{"color": color}, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// ledDo sends one /led/* request and decodes the JSON reply into out.
// A non-2xx reply returns HAL's `detail` as the error.
func ledDo(method, path string, in, out any) error {
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
	resp, err := httpClient.Do(req)
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
