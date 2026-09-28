// Package analytics posts device events to Autonomous Analytics (platform "device").
package analytics

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"sync"
	"time"

	"github.com/joho/godotenv"
)

const (
	envFile   = "/opt/hal/.env"
	envKey    = "AUTONOMOUS_ANALYTICS_ID"
	envKeyURL = "AUTONOMOUS_ANALYTICS_URL"
	platform  = "device"
)

var (
	client = &http.Client{Timeout: 10 * time.Second}

	once      sync.Once
	apiKey    string
	fileURL   string // AUTONOMOUS_ANALYTICS_URL as read from envFile
	pseudoID  string
	sessionID string
)

func initOnce() {
	once.Do(func() {
		apiKey = os.Getenv(envKey)
		// fileURL holds only the file value; the process env is read live in Endpoint().
		if kv, err := godotenv.Read(envFile); err == nil {
			if apiKey == "" {
				apiKey = kv[envKey]
			}
			fileURL = kv[envKeyURL]
		}
		pseudoID, _ = os.Hostname()
		sessionID = fmt.Sprintf("%s-%d", pseudoID, time.Now().Unix())
	})
}

// TrackEvent posts one event with params flattened into event_params; callers should log, not propagate, errors.
func TrackEvent(ctx context.Context, name string, params map[string]any) error {
	initOnce()
	url := Endpoint()
	if url == "" {
		return fmt.Errorf("analytics: %s not set in %s", envKeyURL, envFile)
	}
	if apiKey == "" {
		return fmt.Errorf("analytics: %s not set in %s", envKey, envFile)
	}

	eventParams := make([]map[string]string, 0, len(params))
	for k, v := range params {
		if v == nil {
			continue
		}
		eventParams = append(eventParams, map[string]string{
			"key":   k,
			"value": fmt.Sprint(v),
		})
	}

	body, err := json.Marshal(map[string]any{
		"event_name":      name,
		"event_timestamp": time.Now().Unix(),
		"data": map[string]any{
			"session_id":     sessionID,
			"user_pseudo_id": pseudoID,
			"platform":       platform,
			"event_params":   eventParams,
		},
	})
	if err != nil {
		return fmt.Errorf("marshal event: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("new request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", apiKey)

	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("post event: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("post event: status %d", resp.StatusCode)
	}
	return nil
}

// Endpoint returns AUTONOMOUS_ANALYTICS_URL from the process env, then /opt/hal/.env; "" means off.
func Endpoint() string {
	initOnce()
	if u := os.Getenv(envKeyURL); u != "" {
		return u
	}
	return fileURL
}
