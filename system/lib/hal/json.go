package hal

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
)

// doJSON sends one JSON request to HAL and decodes the JSON reply into out;
// a nil `in` sends no body.
// A non-2xx reply returns HAL's `detail` as the error.
func doJSON(method, path string, in, out any) error {
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
		return &StatusError{Method: method, Path: path, Code: resp.StatusCode, Detail: e.Detail}
	}
	if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
		return fmt.Errorf("decode %s: %w", path, err)
	}
	return nil
}

// StatusError is a non-2xx HAL reply; Detail is HAL's `detail` text, if any.
type StatusError struct {
	Method string
	Path   string
	Code   int
	Detail string
}

func (e *StatusError) Error() string {
	if e.Detail != "" {
		return fmt.Sprintf("%s %s returned %d: %s", e.Method, e.Path, e.Code, e.Detail)
	}
	return fmt.Sprintf("%s %s returned %d", e.Method, e.Path, e.Code)
}
