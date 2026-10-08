package hal

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

var inputModeClient = &http.Client{Timeout: 30 * time.Second}

// SetVoiceInputMode waits for HAL's in-process mode transition to finish.
// The caller owns the deadline; no restart or endpoint polling is involved.
func SetVoiceInputMode(ctx context.Context, mode string, wakeword bool) error {
	body, err := json.Marshal(struct {
		Mode     string `json:"mode"`
		Wakeword bool   `json:"wakeword"`
	}{mode, wakeword})
	if err != nil {
		return fmt.Errorf("encode input mode: %w", err)
	}
	req, err := newRequest(http.MethodPost, "/voice/input-mode", bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("create input mode request: %w", err)
	}
	resp, err := inputModeClient.Do(req.WithContext(ctx))
	if err != nil {
		return fmt.Errorf("post input mode: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("HAL input mode returned HTTP %d", resp.StatusCode)
	}
	var result struct {
		Status string `json:"status"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 4096)).Decode(&result); err != nil {
		return fmt.Errorf("decode input mode: %w", err)
	}
	if result.Status != "ok" {
		return fmt.Errorf("HAL input mode returned status %q", result.Status)
	}
	return nil
}
