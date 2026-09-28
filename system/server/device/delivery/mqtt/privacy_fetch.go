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
// /devices/get-message endpoint. Generous enough for cold-start TLS +
// backend lookup, tight enough that a stuck request can't pile up
// goroutines if the backend goes dark.
const privacyFetchTimeout = 30 * time.Second

// privacyFetchPath is appended to config.LLMBaseURL (with the trailing /v1
// stripped — autonomous endpoints sit one level above the OpenAI-compat /v1
// base, same as /oauth/refresh and /connector/refresh-token).
const privacyFetchPath = "/devices/get-message"

// privacyFetchEnvelope mirrors the inner MQTT data envelope shape so the
// REST response can be slotted into env.Data without per-kind code
// awareness. Backend returns `{cmd, kind, data}` here; the device only
// cares about `data`, but Cmd/Kind are kept for cross-check.
type privacyFetchEnvelope struct {
	Cmd  string          `json:"cmd"`
	Kind string          `json:"kind"`
	Data json.RawMessage `json:"data"`
}

// privacyFetchResponse is the outer envelope returned by the backend.
// Matches the standard lamp API response format:
//
//	{ "status": 1, "data": {<envelope>}, "message": "..." }
type privacyFetchResponse struct {
	Status  int                  `json:"status"`
	Message string               `json:"message,omitempty"`
	Data    privacyFetchEnvelope `json:"data"`
}

// handlePrivacyEnvelope acknowledges a privacy-typed MQTT envelope, then
// kicks off an async REST fetch to retrieve the sensitive Data block from
// the backend. Once Data is in hand, it's threaded through the normal
// dispatchData switch — per-kind handlers see exactly what they would have
// seen via the legacy inline path.
//
// Why a separate path: when Data carries OAuth tokens, API keys, or other
// secrets, the broker (currently plain MQTT) and any party with broker
// credentials can read it. Privacy envelopes keep only Kind on the broker;
// the Data block traverses TLS straight from the backend to the device,
// never landing on the broker.
//
// Lifecycle:
//
//	server → device  : {cmd:"data", kind:<k>, type:"privacy"}
//	device → server  : {kind:<k>, status:"received"}          (immediate ack)
//	device → backend : GET <base>/devices/get-message?kind=<k>
//	backend → device : {status:1, data:{cmd, kind, data:{...}}}
//	device           : dispatchData(env with Data populated)
//	device → server  : <handler-specific status: success | failure | ...>
func (h *DeviceMQTTHandler) handlePrivacyEnvelope(env domain.MQTTDataCommand) error {
	if env.Kind == "" {
		slog.Error("privacy: envelope missing kind", "component", "mqtt")
		return h.publishDataResult("", "failure", "privacy envelope missing kind", nil)
	}

	// Ack immediately so the backend stops retrying and can start tracking
	// the in-flight fetch. Best-effort: a publish failure here is logged but
	// the fetch still proceeds.
	if err := h.publishDataResult(env.Kind, domain.MQTTStatusReceived, "", nil); err != nil {
		slog.Error("privacy: received ack publish failed", "component", "mqtt", "kind", env.Kind, "error", err)
	}

	go h.fetchAndDispatchPrivacy(env)
	return nil
}

// fetchAndDispatchPrivacy runs the REST fetch + dispatch in a goroutine so
// the broker callback isn't blocked on HTTP. Failures here publish a
// terminal "failure" status so the backend can correlate and surface to
// the user; per-kind success is owned by the downstream handler.
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

	// Length only — the fetched blob is the reason we route through the
	// privacy path in the first place (credentials, OAuth tokens). The
	// per-kind handler downstream logs safe metadata (channel name,
	// config_keys) so operators can still see something went through.
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

// fetchPrivacyData performs the REST GET against the backend, returning
// the inner Data block ready to be slotted into MQTTDataCommand.Data.
//
// Auth: Authorization: Bearer <LLMAPIKey> + X-Device-ID — same pattern as
// the oauth/connector refresh paths, so the same per-device credential is
// reused.
//
// `channel` disambiguates generic kinds ("add_channel", "channel.refresh_config")
// whose queued data on the backend is per-channel. When empty the URL keeps
// the legacy ?kind=<k> shape so backends that don't yet key on channel keep
// working unchanged.
func (h *DeviceMQTTHandler) fetchPrivacyData(ctx context.Context, kind, channel string) (json.RawMessage, error) {
	base := strings.TrimRight(strings.TrimSpace(h.config.LLMBaseURL), "/")
	if base == "" {
		return nil, errors.New("LLMBaseURL not configured")
	}
	// LLMBaseURL carries a trailing /v1 for OpenAI-compat LLM calls; autonomous
	// endpoints sit one level above. Mirror oauth_refresh.requestTokenRefresh.
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
