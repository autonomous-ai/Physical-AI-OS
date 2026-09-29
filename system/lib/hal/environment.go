package hal

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
)

// GetEnvironmentStatus reads the environmental snapshot, preserving unknown keys and nulls.
// Success means diagnostics were retrieved, not that every reading is fresh.
func GetEnvironmentStatus() (json.RawMessage, error) {
	return GetEnvironmentStatusContext(context.Background())
}

// GetEnvironmentStatusContext allows shutdown to cancel background acquisition.
func GetEnvironmentStatusContext(ctx context.Context) (json.RawMessage, error) {
	req, err := newRequest(http.MethodGet, "/environment/status", nil)
	if err != nil {
		return nil, err
	}
	resp, err := httpClient.Do(req.WithContext(ctx))
	if err != nil {
		return nil, fmt.Errorf("get environment status: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("get environment status: HAL HTTP %d", resp.StatusCode)
	}
	const maxBytes = 64 * 1024
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxBytes+1))
	if err != nil {
		return nil, fmt.Errorf("read environment status: %w", err)
	}
	if len(body) > maxBytes {
		return nil, fmt.Errorf("environment status exceeds %d bytes", maxBytes)
	}
	var snapshot struct {
		State string `json:"state"`
		Stale *bool  `json:"stale"`
	}
	if err := json.Unmarshal(body, &snapshot); err != nil {
		return nil, fmt.Errorf("decode environment status: %w", err)
	}
	if snapshot.State == "" || snapshot.Stale == nil {
		return nil, fmt.Errorf("invalid environment status: state and stale are required")
	}
	return json.RawMessage(body), nil
}
