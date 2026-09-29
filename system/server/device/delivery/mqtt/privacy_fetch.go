package mqtthandler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
)

// privacyFetchTimeout bounds the REST round-trip to the backend's
// /devices/get-message endpoint.
const privacyFetchTimeout = 30 * time.Second

// privacyFetchPath is appended to config.LLMBaseURL (with the trailing /v1
// stripped — autonomous endpoints sit one level above the OpenAI-compat /v1
// base, same as /oauth/refresh and /connector/refresh-token).
const privacyFetchPath = "/devices/get-message"

// privacyFetchEnvelope mirrors the inner MQTT data envelope shape so the REST
// response can be slotted into env.Data without per-kind code awareness.
type privacyFetchEnvelope struct {
	Cmd  string          `json:"cmd"`
	Kind string          `json:"kind"`
	Data json.RawMessage `json:"data"`
}

// privacyFetchResponse is the outer envelope returned by the backend
// ({status, data, message}).
type privacyFetchResponse struct {
	Status  int                  `json:"status"`
	Message string               `json:"message,omitempty"`
	Data    privacyFetchEnvelope `json:"data"`
}

// handlePrivacyEnvelope acknowledges a privacy-typed MQTT envelope, then
// kicks off an async REST fetch to retrieve the sensitive Data block from the
// backend.
func (h *DeviceMQTTHandler) handlePrivacyEnvelope(env domain.MQTTDataCommand) error {
	if env.Kind == "" {
		slog.Error("privacy: envelope missing kind", "component", "mqtt")
		return h.publishDataResult("", "failure", "privacy envelope missing kind", nil)
	}

	if err := h.publishDataResult(env.Kind, domain.MQTTStatusReceived, "", nil); err != nil {
		slog.Error("privacy: received ack publish failed", "component", "mqtt", "kind", env.Kind, "error", err)
	}

	go h.fetchAndDispatchPrivacy(env)
	return nil
}

// fetchAndDispatchPrivacy runs the REST fetch + dispatch in a goroutine so
// the broker callback isn't blocked on HTTP.
func (h *DeviceMQTTHandler) fetchAndDispatchPrivacy(env domain.MQTTDataCommand) {
	ctx, cancel := context.WithTimeout(context.Background(), privacyFetchTimeout)
	defer cancel()

	slog.Info("privacy envelope: fetching data",
		"component", "mqtt", "kind", env.Kind, "channel_hint", env.Channel)
	data, err := h.fetchPrivacyData(ctx, env.Kind, env.Channel)
	if err != nil {
		slog.Error("privacy fetch failed", "component", "mqtt", "kind", env.Kind, "channel", env.Channel, "error", err)
		_ = h.publishDataResult(env.Kind, "failure", "fetch privacy data: "+err.Error(), nil)
		return
	}

	slog.Info("privacy envelope: data fetched",
		"component", "mqtt", "kind", env.Kind, "channel_hint", env.Channel,
		"data_len", len(data))

	// Replace Data with the fetched payload and clear Type so downstream
	// code paths don't try to re-route this as a privacy envelope.
	env.Data = data
	env.Type = ""
	if err := h.dispatchData(env); err != nil {
		slog.Error("privacy dispatch returned error", "component", "mqtt", "kind", env.Kind, "error", err)
	}
}

// fetchPrivacyData performs the REST GET against the backend, returning the
// inner Data block ready to be slotted into MQTTDataCommand.Data.
func (h *DeviceMQTTHandler) fetchPrivacyData(ctx context.Context, kind, channel string) (json.RawMessage, error) {
	base := strings.TrimRight(strings.TrimSpace(h.config.LLMBaseURL), "/")
	if base == "" {
		return nil, errors.New("LLMBaseURL not configured")
	}
	base = strings.TrimSuffix(base, "/v1")
	endpoint := base + privacyFetchPath + "?kind=" + url.QueryEscape(kind)
	if channel != "" {
		endpoint += "&channel=" + url.QueryEscape(channel)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, fmt.Errorf("new request: %w", err)
	}
	if key := strings.TrimSpace(h.config.LLMAPIKey); key != "" {
		req.Header.Set("Authorization", "Bearer "+key)
	}
	if id := strings.TrimSpace(h.config.DeviceID); id != "" {
		req.Header.Set("X-Device-ID", id)
	}
	req.Header.Set("Accept", "application/json")

	client := &http.Client{Timeout: privacyFetchTimeout}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("http: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read body: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("http %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}

	var parsed privacyFetchResponse
	if err := json.Unmarshal(body, &parsed); err != nil {
		return nil, fmt.Errorf("decode response: %w", err)
	}
	if parsed.Status != 1 {
		if parsed.Message != "" {
			return nil, fmt.Errorf("backend rejected: %s", parsed.Message)
		}
		return nil, fmt.Errorf("backend status=%d", parsed.Status)
	}
	if len(parsed.Data.Data) == 0 {
		return nil, errors.New("backend response missing data block")
	}
	// Sanity-check: if the backend echoed a different kind, surface it —
	// indicates a routing bug somewhere upstream and we'd otherwise silently
	// dispatch the wrong handler.
	if parsed.Data.Kind != "" && parsed.Data.Kind != kind {
		return nil, fmt.Errorf("backend returned kind=%q for requested kind=%q", parsed.Data.Kind, kind)
	}
	return parsed.Data.Data, nil
}
