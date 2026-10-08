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

	"go.autonomous.ai/os/system/domain"
)

const (
	// oauthRefreshInterval is how often the loop scans access_tokens.json for
	// soon-to-expire tokens.
	oauthRefreshInterval = 3 * time.Minute

	// oauthRefreshSkew refreshes a token once it has less than this
	// remaining, so downstream consumers (gws CLI / OpenClaw gateway) never
	// read an already-expired access token.
	oauthRefreshSkew = 10 * time.Minute

	// oauthRefreshTimeout bounds a single refresh round-trip to the backend.
	oauthRefreshTimeout = 30 * time.Second

	// oauthRefreshPath is appended to config.LLMBaseURL.
	oauthRefreshPath = "/oauth/refresh"

	// defaultOAuthTokenLifetime is assumed when a token arrives (or
	// refreshes) without any expiry info.
	defaultOAuthTokenLifetime = time.Hour
)

// errOAuthInvalidGrant marks a refresh the backend rejected as invalid_grant
// — the refresh_token is revoked/expired and retrying won't help until the
// user re-authorizes (a fresh oauth.set).
var errOAuthInvalidGrant = errors.New("oauth refresh: invalid_grant")

// oauthRefreshableProviders lists providers the backend can refresh today.
// Tokens for other providers are left untouched even if near expiry.
var oauthRefreshableProviders = map[string]bool{"google": true}

// oauthRefreshResult is the subset of the backend's response we apply locally.
type oauthRefreshResult struct {
	AccessToken string `json:"access_token"`
	ExpiresIn   int    `json:"expires_in"`
	TokenType   string `json:"token_type"`
	Scope       string `json:"scope"`
}

// resolveExpiresAt returns the absolute expiry (unix seconds) for a token.
func resolveExpiresAt(expiresAt int64, expiresIn int, now time.Time) int64 {
	if expiresAt > 0 {
		return expiresAt
	}
	if expiresIn > 0 {
		return now.Add(time.Duration(expiresIn) * time.Second).Unix()
	}
	// No expiry info: assume the provider default rather than 0, so the refresh
	// loop never treats a freshly-stored token as already-expired.
	return now.Add(defaultOAuthTokenLifetime).Unix()
}

// needsRefresh reports whether a stored token should be proactively
// refreshed.
func needsRefresh(entry domain.OAuthTokenEntry, now time.Time, skew time.Duration) bool {
	if entry.RefreshToken == "" || entry.RefreshRevoked {
		return false
	}
	if entry.ExpiresAt == 0 {
		return true
	}
	return now.Add(skew).Unix() >= entry.ExpiresAt
}

// StartOAuthRefreshLoop runs until ctx is cancelled, periodically refreshing
// near-expiry OAuth access tokens stored in access_tokens.json.
func (h *DeviceMQTTHandler) StartOAuthRefreshLoop(ctx context.Context) {
	h.safeRefreshTick(ctx) // eager first pass on boot
	ticker := time.NewTicker(oauthRefreshInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			h.safeRefreshTick(ctx)
		}
	}
}

// safeRefreshTick runs one refresh pass guarded by a panic recover so a single
// bad iteration only kills the tick, not the whole loop.
func (h *DeviceMQTTHandler) safeRefreshTick(ctx context.Context) {
	defer func() {
		if r := recover(); r != nil {
			slog.Error("oauth-refresh: panic in refresh tick", "component", "mqtt", "panic", r)
		}
	}()
	h.refreshExpiringTokens(ctx)
}

// refreshExpiringTokens performs one scan-and-refresh pass.
func (h *DeviceMQTTHandler) refreshExpiringTokens(ctx context.Context) {
	tokens, err := h.loadAccessTokens()
	if err != nil {
		slog.Error("oauth-refresh: load tokens", "component", "mqtt", "error", err)
		return
	}

	now := time.Now()
	for provider, entry := range tokens.Providers {
		if !oauthRefreshableProviders[provider] || !needsRefresh(entry, now, oauthRefreshSkew) {
			continue
		}

		res, err := h.requestTokenRefresh(ctx, provider, entry.RefreshToken)
		if err != nil {
			slog.Error("oauth-refresh: refresh failed", "component", "mqtt", "provider", provider, "error", err)
			// A revoked refresh_token will never succeed until re-auth, so
			// mark the entry so the loop stops retrying it.
			if errors.Is(err, errOAuthInvalidGrant) {
				revoked := entry
				revoked.RefreshRevoked = true
				if uerr := h.upsertOAuthEntry(provider, revoked); uerr != nil {
					slog.Error("oauth-refresh: mark revoked failed", "component", "mqtt", "provider", provider, "error", uerr)
				}
			}
			h.alertOAuthStateChange(provider, "fail", err.Error())
			continue
		}

		refreshedAt := time.Now()
		updated := entry
		updated.AccessToken = res.AccessToken
		updated.ExpiresAt = resolveExpiresAt(0, res.ExpiresIn, refreshedAt)
		updated.ObtainedAt = refreshedAt.Unix()
		if res.TokenType != "" {
			updated.TokenType = res.TokenType
		}

		if err := h.upsertOAuthEntry(provider, updated); err != nil {
			slog.Error("oauth-refresh: persist refreshed token", "component", "mqtt", "provider", provider, "error", err)
			continue
		}
		slog.Info("oauth-refresh: access token refreshed", "component", "mqtt", "provider", provider, "expires_at", updated.ExpiresAt)
		h.alertOAuthStateChange(provider, "ok", "")
	}
}

// isInvalidGrantResponse reports whether a non-2xx refresh response means the
// refresh_token is permanently revoked.
func isInvalidGrantResponse(statusCode int, body []byte) bool {
	if statusCode != http.StatusUnauthorized {
		return false
	}
	var parsed struct {
		Error struct {
			Type string `json:"type"`
		} `json:"error"`
	}
	if err := json.Unmarshal(body, &parsed); err != nil {
		return false
	}
	return parsed.Error.Type == "invalid_grant"
}

// requestTokenRefresh POSTs the refresh_token to the backend and returns the
// fresh token.
func (h *DeviceMQTTHandler) requestTokenRefresh(ctx context.Context, provider, refreshToken string) (oauthRefreshResult, error) {
	var out oauthRefreshResult

	// The refresh token only ever goes to our own backend, never to a
	// user-chosen LLM provider.
	base, key, ok := h.config.AutonomousBackend()
	if !ok {
		return out, errors.New("no Autonomous backend configured")
	}
	base = strings.TrimSuffix(base, "/v1")

	payload, err := json.Marshal(map[string]string{"provider": provider, "refresh_token": refreshToken})
	if err != nil {
		return out, fmt.Errorf("marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+oauthRefreshPath, bytes.NewReader(payload))
	if err != nil {
		return out, fmt.Errorf("new request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+key)
	if id := strings.TrimSpace(h.config.DeviceID); id != "" {
		req.Header.Set("X-Device-ID", id)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	client := &http.Client{Timeout: oauthRefreshTimeout}
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
		if isInvalidGrantResponse(resp.StatusCode, body) {
			return out, fmt.Errorf("%w: http %d: %s", errOAuthInvalidGrant, resp.StatusCode, strings.TrimSpace(string(body)))
		}
		return out, fmt.Errorf("http %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return out, fmt.Errorf("decode response: %w", err)
	}
	if out.AccessToken == "" {
		return out, errors.New("backend response missing access_token")
	}
	// expires_in must be > 0, or the next tick would re-refresh immediately.
	if out.ExpiresIn <= 0 {
		return out, fmt.Errorf("backend response invalid expires_in=%d", out.ExpiresIn)
	}
	return out, nil
}
