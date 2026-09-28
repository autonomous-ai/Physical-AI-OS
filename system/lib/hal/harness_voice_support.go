package hal

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// SupportsHarnessVoiceMode checks configured input support, not transient sensor health.
// Older HALs, malformed replies and unavailable HALs cannot authorize the mode.
func SupportsHarnessVoiceMode(parent context.Context) (bool, error) {
	ctx, cancel := context.WithTimeout(parent, time.Second)
	defer cancel()
	req, err := newRequest(http.MethodGet, "/device", nil)
	if err != nil {
		return false, fmt.Errorf("build HAL device request: %w", err)
	}
	resp, err := httpClient.Do(req.WithContext(ctx))
	if err != nil {
		return false, fmt.Errorf("read HAL device: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return false, fmt.Errorf("HAL device returned HTTP %d", resp.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 65537))
	if err != nil {
		return false, fmt.Errorf("read HAL device body: %w", err)
	}
	if len(raw) > 65536 {
		return false, fmt.Errorf("HAL device body exceeds limit")
	}
	var device struct {
		Inputs struct {
			MPR121 bool `json:"mpr121"`
		} `json:"inputs"`
	}
	if err := json.Unmarshal(raw, &device); err != nil {
		return false, fmt.Errorf("decode HAL device: %w", err)
	}
	return device.Inputs.MPR121, nil
}
