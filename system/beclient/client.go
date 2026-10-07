package beclient

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/clocksync"
	"go.autonomous.ai/os/system/server/config"
)

const (
	// DefaultTimeout is the HTTP client timeout for API requests.
	DefaultTimeout = 15 * time.Second
	// StatusReportInterval is how often to ping status to the backend.
	StatusReportInterval = 15 * time.Second
)

// Client calls the autonomous backend API to report setup status and device status.
// Base URL is read from config.LLMBaseURL on each Ping.
type Client struct {
	config     *config.Config
	httpClient *http.Client
	// cachedSlackTeamID is the Slack workspace ID from auth.test; empty if unresolved.
	cachedSlackTeamID atomic.Value // string
}

// New creates a new BE client. Base URL is read from cfg.LLMBaseURL on each request.
func New(cfg *config.Config) *Client {
	// Own transport so CloseIdleConnections does not affect other HTTP users.
	transport := http.DefaultTransport.(*http.Transport).Clone()
	// 30s keeps reuse across the 15s ping cadence while limiting stale-network reuse.
	transport.IdleConnTimeout = 30 * time.Second
	c := &Client{
		config: cfg,
		httpClient: &http.Client{
			Timeout:   DefaultTimeout,
			Transport: transport,
		},
	}
	c.cachedSlackTeamID.Store("")
	return c
}

// CloseIdleConnections drops pooled connections; call it when the LAN address
// changes, since stale connections otherwise hang until the client timeout.
func (c *Client) CloseIdleConnections() {
	c.httpClient.CloseIdleConnections()
}

// SetSlackTeamID records the Slack workspace ID resolved for the device's
// bot_token. Idempotent — calling with the same value is a no-op.
func (c *Client) SetSlackTeamID(v string) {
	c.cachedSlackTeamID.Store(v)
}

// SlackTeamID returns the cached Slack workspace ID. Empty string when not
// yet resolved or no slack channel is configured.
func (c *Client) SlackTeamID() string {
	v, _ := c.cachedSlackTeamID.Load().(string)
	return v
}

// ResolveSlackTeamIDFromConfig caches the Slack team_id via auth.test when a bot
// token is configured. Safe to call every tick; failures retry next tick.
func (c *Client) ResolveSlackTeamIDFromConfig(configDir string) {
	if c.SlackTeamID() != "" {
		return
	}
	botToken := readSlackBotTokenFromConfig(configDir)
	if botToken == "" {
		return // slack not configured on this device — nothing to resolve
	}
	teamID, err := slackAuthTest(c.httpClient, botToken)
	if err != nil {
		slog.Debug("slack auth.test failed (will retry next tick)", "component", "beclient", "error", err)
		return
	}
	if teamID == "" {
		return
	}
	c.SetSlackTeamID(teamID)
	slog.Info("resolved slack team_id for ping payload", "component", "beclient", "team_id", teamID)
}

// readSlackBotTokenFromConfig reads channels.slack.botToken from openclaw.json;
// empty on any failure.
func readSlackBotTokenFromConfig(configDir string) string {
	if configDir == "" {
		return ""
	}
	raw, err := os.ReadFile(filepath.Join(configDir, "openclaw.json"))
	if err != nil {
		return ""
	}
	var doc struct {
		Channels struct {
			Slack struct {
				BotToken string `json:"botToken"`
			} `json:"slack"`
		} `json:"channels"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return ""
	}
	return doc.Channels.Slack.BotToken
}

// slackAuthTest calls Slack auth.test with the bot token and returns team_id.
func slackAuthTest(httpClient *http.Client, botToken string) (string, error) {
	req, err := http.NewRequest(http.MethodPost, "https://slack.com/api/auth.test", nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+botToken)
	resp, err := httpClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", err
	}
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("auth.test http %d", resp.StatusCode)
	}
	var out struct {
		OK     bool   `json:"ok"`
		Error  string `json:"error,omitempty"`
		TeamID string `json:"team_id"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return "", err
	}
	if !out.OK {
		return "", fmt.Errorf("auth.test not ok: %s", out.Error)
	}
	return out.TeamID, nil
}

// Ping posts payload to the backend /ping with token as Bearer. Adds ?mqtt=true
// when MQTT is unconfigured so the response includes MQTT config.
func (c *Client) Ping(token string, payload PingPayload) (*PingResponse, error) {
	base := strings.TrimSuffix(c.config.BackendBase(), "/")
	if base == "" || token == "" {
		return nil, nil
	}
	// /ping lives one level above the OpenAI-compat /v1 base.
	base = strings.TrimSuffix(base, "/v1")
	pingURL := base + "/ping"
	if strings.TrimSpace(c.config.MQTTEndpoint) == "" {
		pingURL += "?mqtt=true"
	}
	body, _ := json.Marshal(payload)
	slog.Debug("pinging backend", "component", "beclient", "url", pingURL, "body", string(body))
	return c.postWithAuth(pingURL, token, payload)
}

// PingPayload is the ping body; device-state fields mirror the MQTT `info` uplink.
type PingPayload struct {
	Status         string `json:"status,omitempty"`
	SetupCompleted bool   `json:"setup_completed,omitempty"`
	Mac            string `json:"mac,omitempty"`     // Hardware ID (<device_type>-XXXX from Pi serial)
	Version        string `json:"version,omitempty"` // App version for OTA comparison
	// SlackTeamID lets the backend route inbound Slack events to this device.
	SlackTeamID string `json:"slack_team_id,omitempty"`
	// LocalIP is the STA LAN address, used by the post-setup redirect rescue.
	LocalIP  string `json:"local_ip,omitempty"`
	Device   string `json:"device,omitempty"`    // Device type (lamp, intern, …)
	DeviceID string `json:"device_id,omitempty"` // Backend-issued device id from config
	Timezone string `json:"timezone,omitempty"`  // Live IANA zone (/etc/timezone)
	// Active agentic runtime and its version only.
	AgentRuntime        string `json:"agent_runtime,omitempty"`
	AgentRuntimeVersion string `json:"agent_runtime_version,omitempty"`
	HalVersion          string `json:"hal_version,omitempty"`
	// Voice/STT config, mirroring the MQTT `info` fields.
	TTSProvider string `json:"tts_provider,omitempty"`
	TTSVoice    string `json:"tts_voice,omitempty"`
	STTLanguage string `json:"stt_language,omitempty"`
	// WakeWordEnabled is never omitted so the state is always explicit.
	WakeWordEnabled bool   `json:"wakeword_enabled"`
	VoiceInputMode  string `json:"voice_input_mode"`
	// UnsupportedChannels lists configured channels the active runtime cannot
	// run (populated by ChannelReconcile after a runtime switch).
	UnsupportedChannels []string `json:"unsupported_channels,omitempty"`
	// Skills installed in the active runtime (name+description only).
	Skills []domain.SkillSummary `json:"skills,omitempty"`
}

// MQTTConfig holds MQTT broker configuration from the backend.
type MQTTConfig struct {
	Endpoint  string `json:"mqtt_server,omitempty"`
	Port      string `json:"mqtt_port,omitempty"`
	Username  string `json:"mqtt_usr,omitempty"`
	Password  string `json:"mqtt_pwd,omitempty"`
	FaChannel string `json:"fa_channel,omitempty"`
	FdChannel string `json:"fd_channel,omitempty"`
}

// PingResponse is the backend response to a ping.
type PingResponse struct {
	Status   string      `json:"status"`
	DeviceID string      `json:"device_id,omitempty"`
	MQTT     *MQTTConfig `json:"mqtt,omitempty"`
}

// HasMQTT returns true if the response contains MQTT configuration.
func (r *PingResponse) HasMQTT() bool {
	return r != nil && r.MQTT != nil && strings.TrimSpace(r.MQTT.Endpoint) != ""
}

// GetMQTT returns the MQTT config or nil.
func (r *PingResponse) GetMQTT() *MQTTConfig {
	if r == nil {
		return nil
	}
	return r.MQTT
}

func (c *Client) postWithAuth(reqURL, bearerToken string, body any) (*PingResponse, error) {
	var bodyReader *bytes.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("marshal body: %w", err)
		}
		bodyReader = bytes.NewReader(data)
	} else {
		bodyReader = bytes.NewReader([]byte("{}"))
	}
	req, err := http.NewRequest(http.MethodPost, reqURL, bodyReader)
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+bearerToken)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		// A stale clock rejects every certificate; resync instead of retrying blind.
		if clocksync.IsClockError(err) {
			clocksync.Kick("ping_tls")
		}
		return nil, fmt.Errorf("request %s: %w", reqURL, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("request %s: status %d", reqURL, resp.StatusCode)
	}

	var pingResp PingResponse
	if err := json.NewDecoder(resp.Body).Decode(&pingResp); err != nil {
		return nil, nil
	}
	return &pingResp, nil
}

// PingSafe logs errors but does not propagate them. Returns the response if available.
func (c *Client) PingSafe(token string, payload PingPayload) *PingResponse {
	resp, err := c.Ping(token, payload)
	if err != nil {
		slog.Error("ping failed", "component", "beclient", "error", err)
	}
	return resp
}
