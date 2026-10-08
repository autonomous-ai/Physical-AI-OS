package mqtthandler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"
)

const (
	// connectorRefreshInterval is how often the loop scans every registered
	// ConnectorWriter for expiring entries.
	connectorRefreshInterval = 3 * time.Minute

	// connectorRefreshSkew refreshes a token once it has less than this
	// remaining.
	connectorRefreshSkew = 10 * time.Minute

	// connectorRefreshTimeout bounds a single refresh round-trip to the backend.
	connectorRefreshTimeout = 30 * time.Second

	// connectorRefreshPath is appended to config.LLMBaseURL (minus /v1).
	connectorRefreshPath = "/connector/refresh-token"
)

// connectorRefreshResult is the subset of the backend response we apply
// locally.
type connectorRefreshResult struct {
	AccessToken  string `json:"access_token"`
	RefreshToken string `json:"refresh_token"`
	TokenType    string `json:"token_type"`
	ExpiresIn    int    `json:"expires_in"`
	Scope        string `json:"scope"`
}

// StartConnectorRefreshLoop runs until ctx is cancelled, periodically
// scanning the writer registry for tokens nearing expiry and refreshing them
// through the backend `/connector/refresh-token` endpoint.
func (h *DeviceMQTTHandler) StartConnectorRefreshLoop(ctx context.Context) {
	h.safeConnectorRefreshTick(ctx) // eager first pass on boot
	ticker := time.NewTicker(connectorRefreshInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			h.safeConnectorRefreshTick(ctx)
		}
	}
}

func (h *DeviceMQTTHandler) safeConnectorRefreshTick(ctx context.Context) {
	defer func() {
		if r := recover(); r != nil {
			slog.Error("connector-refresh: panic in refresh tick", "component", "mqtt", "panic", r)
		}
	}()
	h.refreshExpiringConnectors(ctx)
}

// refreshExpiringConnectors iterates every registered writer, asks for its
// refreshable entries, and proactively rotates any that fall inside the skew
// window.
func (h *DeviceMQTTHandler) refreshExpiringConnectors(ctx context.Context) {
	now := time.Now()
	for _, w := range h.refreshableConnectorWriters() {
		for _, target := range w.RefreshableEntries() {
			if !connectorNeedsRefresh(target, now, connectorRefreshSkew) {
				continue
			}
			res, err := h.requestConnectorTokenRefresh(ctx, target.Connector, target.RefreshToken)
			if err != nil {
				slog.Error("connector-refresh: refresh failed", "component", "mqtt", "connector", target.Connector, "error", err)
				continue
			}
			creds, ok, err := h.loadRefreshedCreds(w, target.Connector, res, now)
			if err != nil {
				slog.Error("connector-refresh: build creds", "component", "mqtt", "connector", target.Connector, "error", err)
				continue
			}
			if !ok {
				// Race: entry vanished between the listing and the load
				// (a concurrent connector.remove). Skip silently.
				continue
			}
			if err := w.Write(ctx, creds); err != nil {
				slog.Error("connector-refresh: persist refreshed", "component", "mqtt", "connector", target.Connector, "error", err)
				continue
			}
			slog.Info("connector-refresh: token refreshed", "component", "mqtt", "connector", target.Connector, "expires_at", creds.ExpiresAt)
		}
	}
}

// connectorNeedsRefresh reports whether the entry should be proactively
// refreshed.
func connectorNeedsRefresh(t ConnectorRefreshTarget, now time.Time, skew time.Duration) bool {
	if t.RefreshToken == "" || t.ExpiresAt == 0 {
		return false
	}
	return now.Add(skew).Unix() >= t.ExpiresAt
}

// entryLoader is implemented by the per-connector MCP writer
// (mcpConnectorWriter) which keeps a full on-disk entry per connector.
type entryLoader interface {
	loadEntry(connector string) (ConnectorCreds, bool, error)
}

// loadRefreshedCreds builds the ConnectorCreds the writer needs for a
// re-Write.
func (h *DeviceMQTTHandler) loadRefreshedCreds(w ConnectorWriter, connector string, res connectorRefreshResult, refreshedAt time.Time) (ConnectorCreds, bool, error) {
	if el, ok := w.(entryLoader); ok {
		base, present, err := el.loadEntry(connector)
		if err != nil {
			return ConnectorCreds{}, false, err
		}
		if !present {
			return ConnectorCreds{}, false, nil
		}
		base.AccessToken = res.AccessToken
		base.TokenType = firstNonEmpty(res.TokenType, base.TokenType)
		base.ExpiresAt = resolveExpiresAt(0, res.ExpiresIn, refreshedAt)
		if res.RefreshToken != "" {
			base.RefreshToken = res.RefreshToken
		}
		base.ObtainedAt = refreshedAt.Unix()
		return base, true, nil
	}
	return ConnectorCreds{
		Connector:    connector,
		AccessToken:  res.AccessToken,
		RefreshToken: res.RefreshToken,
		TokenType:    res.TokenType,
		ExpiresAt:    resolveExpiresAt(0, res.ExpiresIn, refreshedAt),
		ObtainedAt:   refreshedAt.Unix(),
	}, true, nil
}

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

// requestConnectorTokenRefresh POSTs the refresh_token to the backend and
// returns the fresh token.
func (h *DeviceMQTTHandler) requestConnectorTokenRefresh(ctx context.Context, connector, refreshToken string) (connectorRefreshResult, error) {
	var out connectorRefreshResult

	// The refresh token only ever goes to our own backend, never to a
	// user-chosen LLM provider.
	base, key, ok := h.config.AutonomousBackend()
	if !ok {
		return out, errors.New("no Autonomous backend configured")
	}
	base = strings.TrimSuffix(base, "/v1")

	payload, err := json.Marshal(map[string]string{"connector": connector, "refresh_token": refreshToken})
	if err != nil {
		return out, fmt.Errorf("marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+connectorRefreshPath, bytes.NewReader(payload))
	if err != nil {
		return out, fmt.Errorf("new request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+key)
	if id := strings.TrimSpace(h.config.DeviceID); id != "" {
		req.Header.Set("X-Device-ID", id)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	client := &http.Client{Timeout: connectorRefreshTimeout}
	resp, err := client.Do(req)
	if err != nil {
		return out, fmt.Errorf("http: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return out, fmt.Errorf("read body: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return out, fmt.Errorf("http %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return out, fmt.Errorf("decode response: %w", err)
	}
	if out.AccessToken == "" {
		return out, errors.New("backend response missing access_token")
	}
	// expires_in must be strictly positive — see oauth_refresh.go's same guard:
	// a 0 would store ExpiresAt = now, spinning the loop every tick.
	if out.ExpiresIn <= 0 {
		return out, fmt.Errorf("backend response invalid expires_in=%d", out.ExpiresIn)
	}
	return out, nil
}
