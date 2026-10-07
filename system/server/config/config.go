package config

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"

	"go.autonomous.ai/os/system/lib/mqtt"
	"go.autonomous.ai/os/system/lib/syspath"
	"go.autonomous.ai/os/system/lib/urlnorm"
)

// otaMetadataURLFromBootstrap returns metadata_url from the OTA worker's
// config file, or "" when it is missing or invalid (e.g. device not yet
// provisioned).
func otaMetadataURLFromBootstrap() string {
	data, err := os.ReadFile(syspath.BootstrapConfig())
	if err != nil {
		return ""
	}
	var bc struct {
		MetadataURL string `json:"metadata_url"`
	}
	if err := json.Unmarshal(data, &bc); err != nil {
		return ""
	}
	return strings.TrimSpace(bc.MetadataURL)
}

var configPath = "config/config.json"

// Path returns the absolute path of config.json.
func Path() string {
	if abs, err := filepath.Abs(configPath); err == nil {
		return abs
	}
	return configPath
}

// Dir returns the directory config.json lives in.
func Dir() string {
	return filepath.Dir(Path())
}

// OSVersion is injected at build time via ldflags
// (-X go.autonomous.ai/os/system/server/config.OSVersion=v1.2.3).
var OSVersion = "dev"

// MCPTool is a remote MCP tool endpoint the agent can call over HTTPS.
type MCPTool struct {
	// Name is the short identifier used as the mcp.servers.<name> key in
	// openclaw.json (e.g. "search", "weather"). Must be unique.
	Name string `json:"name" yaml:"name"`
	// URL is the remote MCP endpoint (e.g.
	// "https://owner-space.hf.space/gradio_api/mcp/").
	URL string `json:"url" yaml:"url"`
	// Headers are optional HTTP headers sent with every MCP request.
	// Common uses: {"Authorization": "Bearer sk-..."} or {"X-API-Key": "..."}.
	Headers map[string]string `json:"headers,omitempty" yaml:"headers,omitempty"`
}

type Config struct {
	// mu serialises LLMModel mutations and config.Save() so the primary-model
	// watcher goroutine (syncPrimaryFromFile) cannot race with HTTP handlers
	// (device.UpdateConfig) that set LLMModel concurrently.
	mu sync.Mutex

	HttpPort int `json:"httpPort" yaml:"httpPort" validate:"required"`

	// Channel type: "telegram" or "slack" (empty defaults to telegram for backward compat)
	Channel string `json:"channel" yaml:"channel"`

	TelegramBotToken string `json:"telegram_bot_token" yaml:"telegramBotToken"`
	TelegramUserID   string `json:"telegram_user_id" yaml:"telegramUserID"`

	SlackBotToken string `json:"slack_bot_token" yaml:"slackBotToken"`
	SlackAppToken string `json:"slack_app_token" yaml:"slackAppToken"`
	SlackUserID   string `json:"slack_user_id" yaml:"slackUserID"`

	DiscordBotToken string `json:"discord_bot_token" yaml:"discordBotToken"`
	DiscordGuildID  string `json:"discord_guild_id" yaml:"discordGuildID"`
	DiscordUserID   string `json:"discord_user_id" yaml:"discordUserID"`

	// WhatsappUserID is the E.164 phone number permitted to DM the device's
	// WhatsApp account.
	WhatsappUserID string `json:"whatsapp_user_id" yaml:"whatsappUserID"`

	// iMessage via BlueBubbles.
	BluebubblesServerURL   string `json:"bluebubbles_server_url" yaml:"bluebubblesServerURL"`
	BluebubblesPassword    string `json:"bluebubbles_password" yaml:"bluebubblesPassword"`
	BluebubblesUserAddress string `json:"bluebubbles_user_address" yaml:"bluebubblesUserAddress"`
	// BluebubblesCallerContext is an optional admin-supplied prompt telling the
	// LLM how to treat incoming iMessage traffic.
	BluebubblesCallerContext string `json:"bluebubbles_caller_context" yaml:"bluebubblesCallerContext"`

	// ChannelsAppliedRuntime is the agent runtime ChannelReconcile last
	// applied the configured channels for.
	ChannelsAppliedRuntime string `json:"channels_applied_runtime,omitempty" yaml:"channelsAppliedRuntime"`
	// ChannelsUnsupported lists channels configured here that the active runtime
	// cannot run, set by ChannelReconcile and surfaced on the MQTT info uplink.
	ChannelsUnsupported []string `json:"channels_unsupported,omitempty" yaml:"channelsUnsupported"`

	// UserProfileReconcile enables the startup pass that retires a person
	// from every runtime's USER.md once they no longer have a face/voice
	// enrollment (see agent.UserProfileReconcile).
	UserProfileReconcile *bool `json:"user_profile_reconcile,omitempty" yaml:"userProfileReconcile"`

	// MemoryGuard enables the sweep that quarantines self-written agent
	// memory which could steer routing (#421). nil/true = write; false = log.
	MemoryGuard *bool `json:"memory_guard,omitempty" yaml:"memoryGuard"`

	// MCPAppliedRuntime is the agent runtime MCPReconcile last cloned the
	// configured MCP connectors for.
	MCPAppliedRuntime string `json:"mcp_applied_runtime,omitempty" yaml:"mcpAppliedRuntime"`

	// LLMConfigAppliedRuntime is the agent runtime ConfigMigration last
	// successfully migrated LLM config for. Separate from agent_state.json so
	// a failed migration retries on the next boot.
	LLMConfigAppliedRuntime string `json:"llm_config_applied_runtime,omitempty" yaml:"llmConfigAppliedRuntime"`

	LLMAPIKey  string `json:"llm_api_key" yaml:"llmAPIKey" validate:"required"`
	LLMModel   string `json:"llm_model" yaml:"llmModel" validate:"required"`
	LLMBaseURL string `json:"llm_base_url" yaml:"llmBaseURL" validate:"required"`

	// BackendBaseURL / BackendAPIKey point the Autonomous backend channel —
	// the /ping status report (which also delivers MQTT endpoint updates) and
	// ops /alert — somewhere other than the LLM endpoint.
	BackendBaseURL string `json:"backend_base_url,omitempty" yaml:"backendBaseURL"`
	BackendAPIKey  string `json:"backend_api_key,omitempty" yaml:"backendAPIKey"`

	// AutonomousDefaults preserves the shipped credential set. Captured once on
	// the first credential change; only a factory reset clears it.
	AutonomousDefaults *AutonomousDefaults `json:"autonomous_defaults,omitempty" yaml:"autonomousDefaults"`

	// AlertsDisabled mutes device ops-alerts to bff-campaign-service (POST
	// {LLMBaseURL}/alert).
	AlertsDisabled bool `json:"alerts_disabled,omitempty" yaml:"alertsDisabled"`

	// ClaudeCodeOAuthToken is the long-lived claude.ai OAuth token produced
	// by the claudecode login flow (`claude setup-token`,
	// runtimes/claudecode/login.go).
	ClaudeCodeOAuthToken string `json:"claude_code_oauth_token,omitempty" yaml:"claudeCodeOAuthToken"`

	// DefaultModelVersion is the upstream model-catalog version last applied
	// by the set-default-model flow (setup + periodic sync).
	DefaultModelVersion int `json:"default_model_version" yaml:"defaultModelVersion"`
	// STTBaseURL / TTSBaseURL override LLMBaseURL when STT or TTS lives on
	// a different host than the LLM. Empty = reuse LLMBaseURL.
	STTBaseURL string `json:"stt_base_url" yaml:"sttBaseURL"`
	TTSBaseURL string `json:"tts_base_url" yaml:"ttsBaseURL"`

	// OTAMetadataURL is not persisted in config.json — it is sourced at
	// load from /root/config/bootstrap.json (single source of truth, see
	// ProvideConfig).
	OTAMetadataURL string `json:"-" yaml:"-"`

	DeepgramAPIKey string `json:"deepgram_api_key" yaml:"deepgramAPIKey"`
	// STTAPIKey is the API key for the AutonomousSTT (LLM-as-STT) backend
	// used when DeepgramAPIKey is empty.
	STTAPIKey string `json:"stt_api_key" yaml:"sttAPIKey"`
	// TTSAPIKey is the API key for the TTS provider; empty falls back to
	// LLMAPIKey.
	TTSAPIKey       string   `json:"tts_api_key" yaml:"ttsAPIKey"`
	TTSProvider     string   `json:"tts_provider" yaml:"ttsProvider"`
	TTSVoice        string   `json:"tts_voice" yaml:"ttsVoice"`
	TTSSpeed        *float64 `json:"tts_speed,omitempty" yaml:"ttsSpeed"`
	TTSInstructions string   `json:"tts_instructions" yaml:"ttsInstructions"`

	// AgentRuntime selects which agentic backend to use: "openclaw" (default), "hermes", "picoclaw", "claudecode", etc.
	AgentRuntime string `json:"agent_runtime" yaml:"agentRuntime"`

	// AgentRemoteURL + AgentRemoteToken configure the "remote" runtime —
	// the device becomes a voice/chat frontend for a gateway on another
	// machine (typically the user's Mac).
	AgentRemoteURL   string `json:"agent_remote_url,omitempty" yaml:"agentRemoteURL"`
	AgentRemoteToken string `json:"agent_remote_token,omitempty" yaml:"agentRemoteToken"`

	// Realtime configures the realtime voice agent (audio-native brain —
	// Gemini Live / OpenAI Realtime).
	Realtime *RealtimeConfig `json:"realtime,omitempty" yaml:"realtime"`

	// WakeWord gates voice turns before either the realtime model or the main
	// agent sees them.
	WakeWord       *bool  `json:"wakeword,omitempty" yaml:"wakeword"`
	VoiceInputMode string `json:"voice_input_mode,omitempty" yaml:"voice_input_mode"`

	OpenclawConfigDir string `json:"openclaw_config_dir" yaml:"openclawConfigDir"`

	NetworkSSID     string `json:"network_ssid" yaml:"networkSSID" validate:"required"`
	NetworkPassword string `json:"network_password" yaml:"networkPassword" validate:"required"`

	SetUpCompleted bool `json:"set_up_completed" yaml:"setUpCompleted"`

	// DeviceID is saved at setup, used for backend status reporting
	DeviceID string `json:"device_id" yaml:"deviceID"`

	// Timezone is the IANA zone name (e.g. "Asia/Ho_Chi_Minh") the operator
	// picked in Settings.
	Timezone string `json:"timezone,omitempty" yaml:"timezone"`

	// DeviceType is the device class/profile id — the folder name under
	// robots/ (e.g. "lamp", "intern-v2", "unitree-go2w").
	DeviceType string `json:"device_type,omitempty" yaml:"deviceType"`

	// MQTT (optional): empty broker URL means MQTT disabled
	MQTTEndpoint string `json:"mqtt_endpoint" yaml:"mqttEndpoint"`
	MQTTUsername string `json:"mqtt_username" yaml:"mqttUsername"`
	MQTTPassword string `json:"mqtt_password" yaml:"mqttPassword"`
	MQTTPort     int    `json:"mqtt_port" yaml:"mqttPort"`
	FAChannel    string `json:"fa_channel" yaml:"faChannel"`
	FDChannel    string `json:"fd_channel" yaml:"fdChannel"`

	// LocalIntent enables local keyword matching for common voice commands (default true).
	// When false, all voice commands go through the agent (OpenClaw).
	LocalIntent *bool `json:"local_intent,omitempty" yaml:"localIntent"`
	// JevIntent configures semantic fallback after local rules miss (default off).
	// It shares the configured Autonomous proxy URL and device API key.
	JevIntent *JevIntentConfig `json:"jev_intent,omitempty" yaml:"jevIntent"`

	// JevHarness enables Harness session selection (default true).
	// Uncertain selections and provider failures defer to the main agent.
	JevHarness *JevIntentConfig `json:"jev_harness,omitempty" yaml:"jevHarness"`

	// LLMDisableThinking disables extended thinking/reasoning for all LLM models (default false).
	// Enable this to reduce latency on fast models like Haiku that don't benefit from thinking.
	LLMDisableThinking *bool `json:"llm_disable_thinking,omitempty" yaml:"llmDisableThinking"`

	// STTModel selects the speech-to-text model for hal.
	STTModel string `json:"stt_model,omitempty" yaml:"sttModel"`

	// STTLanguage sets the BCP-47 language code for STT (e.g. "vi", "en").
	// Only used when STTModel is non-empty. Empty means auto-detect.
	STTLanguage string `json:"stt_language,omitempty" yaml:"sttLanguage"`

	// GuardMode enables guard/security mode (default false).
	GuardMode *bool `json:"guard_mode,omitempty" yaml:"guardMode"`

	// SensingTurnFloorS is the minimum gap in seconds between two agent turns
	// initiated by ambient sensing events, across ALL event types. 0 disables.
	SensingTurnFloorS *int `json:"sensing_turn_floor_s,omitempty" yaml:"sensingTurnFloorS"`

	Environment *EnvironmentConfig `json:"environment,omitempty" yaml:"environment"`

	// GuardInstruction is a custom instruction the owner provides when enabling guard mode.
	// Injected into sensing events so the agent follows it (e.g. "play scary sound when stranger detected").
	GuardInstruction string `json:"guard_instruction,omitempty" yaml:"guardInstruction"`

	// MCPTools is the list of remote MCP tool endpoints (HF Spaces, public
	// MCP servers) the agent can call.
	MCPTools []MCPTool `json:"mcp_tools,omitempty" yaml:"mcpTools"`

	// AdminPasswordHash is the bcrypt hash of the admin login password set
	// during device setup.
	AdminPasswordHash string `json:"admin_password_hash,omitempty" yaml:"adminPasswordHash"`

	// SessionSecret is a random 32-byte key (base64) used to sign HMAC
	// session tokens.
	SessionSecret string `json:"session_secret,omitempty" yaml:"sessionSecret"`

	notify chan bool
}

// Load reads config from configPath. Returns *Config, never a value: Config
// carries a sync.Mutex.
func Load() (*Config, error) {
	if _, err := os.Stat(configPath); os.IsNotExist(err) {
		d := Default()
		return &d, fmt.Errorf("config file not found: %s", configPath)
	}
	data, err := os.ReadFile(configPath)
	if err != nil {
		d := Default()
		return &d, fmt.Errorf("read config %s: %w", configPath, err)
	}
	var cfg Config
	if err := json.Unmarshal(data, &cfg); err != nil {
		d := Default()
		return &d, fmt.Errorf("parse config %s: %w", configPath, err)
	}
	cfg.notify = make(chan bool, 1)
	return &cfg, nil
}

func Default() Config {
	environment := DefaultEnvironmentConfig()
	return Config{
		Environment: &environment,
		HttpPort:    5000,

		TelegramBotToken: "",

		LLMAPIKey:  "",
		LLMModel:   "claude-opus-4-6",
		LLMBaseURL: "",

		OTAMetadataURL: "",

		OpenclawConfigDir: "/root/.openclaw",

		NetworkSSID:     "",
		NetworkPassword: "",
		SetUpCompleted:  false,
		DeviceID:        "",

		MQTTEndpoint: "",
		MQTTUsername: "",
		MQTTPassword: "",
		MQTTPort:     0,

		Realtime: DefaultRealtimeConfig(),

		notify: make(chan bool, 1),
	}
}

// BackendBase returns the base URL for the Autonomous backend channel
// (/ping, /alert): BackendBaseURL when set, else LLMBaseURL.
func (c *Config) BackendBase() string {
	if v := strings.TrimSpace(c.BackendBaseURL); v != "" {
		return v
	}
	return strings.TrimSpace(c.LLMBaseURL)
}

// BackendKey returns the bearer token for the backend channel:
// BackendAPIKey when set, else LLMAPIKey.
func (c *Config) BackendKey() string {
	if v := strings.TrimSpace(c.BackendAPIKey); v != "" {
		return v
	}
	return strings.TrimSpace(c.LLMAPIKey)
}

// WakeWordEnabled reports whether STT must first recognize a wake phrase
// before HAL handles the voice turn. Unset defaults to false for upgrades.
func (c *Config) WakeWordEnabled() bool {
	return c.WakeWord != nil && *c.WakeWord
}

// DeviceTypeOrDefault resolves the device class used to pick
// robots/<type>/{DEVICE,SOUL}.md: DEVICE_TYPE env, then config.json. Returns
// "" when unresolved — no "lamp" fallback.
func (c *Config) DeviceTypeOrDefault() string {
	if t := os.Getenv("DEVICE_TYPE"); t != "" {
		return t
	}
	return c.DeviceType
}

func ProvideConfig() *Config {
	if _, err := os.Stat(configPath); os.IsNotExist(err) {
		c := Default()
		if err := c.Save(); err != nil {
			slog.Error("save config failed", "component", "config", "error", err)
		}
		c.notify = make(chan bool, 1)
		c.OTAMetadataURL = otaMetadataURLFromBootstrap()
		return &c
	}

	data, err := os.ReadFile(configPath)
	if err != nil {
		panic(fmt.Errorf("read config %s: %w", configPath, err))
	}

	var cfg Config
	if err := json.Unmarshal(data, &cfg); err != nil {
		panic(fmt.Errorf("parse config %s: %w", configPath, err))
	}
	cfg.notify = make(chan bool, 1)

	if cfg.OpenclawConfigDir == "/root/openclaw" {
		if err := migrateOpenclawDir("/root/openclaw", "/root/.openclaw"); err != nil {
			slog.Error("openclaw dir migration failed", "component", "config", "error", err)
		} else {
			cfg.OpenclawConfigDir = "/root/.openclaw"
			if err := cfg.Save(); err != nil {
				slog.Error("save config after migration failed", "component", "config", "error", err)
			}
		}
	}

	if cfg.HttpPort == 0 {
		cfg.HttpPort = Default().HttpPort
		slog.Warn("config.json has no httpPort — using default",
			"component", "config", "port", cfg.HttpPort)
	}

	// Track fleet defaults until pinned; write only on change, since a rewrite
	// changes the HAL config hash and restarts voice playback.
	if cfg.Realtime == nil || !cfg.Realtime.Pinned {
		def := DefaultRealtimeConfig()
		cur, _ := json.Marshal(cfg.Realtime)
		want, _ := json.Marshal(def)
		if !bytes.Equal(cur, want) {
			cfg.Realtime = def
			if err := cfg.Save(); err != nil {
				slog.Error("seed realtime config failed", "component", "config", "error", err)
			}
		}
	}
	// In-memory default for pre-existing configs; never written, so an upgrade
	// does not change the HAL config hash and restart voice playback.
	if cfg.WakeWord == nil {
		wakeWord := false
		cfg.WakeWord = &wakeWord
	}

	cfg.OTAMetadataURL = otaMetadataURLFromBootstrap()

	return &cfg
}

// WithLockSave is the canonical way to mutate config fields and persist them.
// Mutate, marshal and write happen under one lock so concurrent saves cannot
// land a stale snapshot; notify fires after the lock is released.
func (c *Config) WithLockSave(fn func(*Config)) error {
	c.mu.Lock()
	fn(c)
	c.LLMBaseURL = urlnorm.NormalizeBaseURL(c.LLMBaseURL)
	c.STTBaseURL = urlnorm.NormalizeBaseURL(c.STTBaseURL)
	c.TTSBaseURL = urlnorm.NormalizeBaseURL(c.TTSBaseURL)
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		c.mu.Unlock()
		return fmt.Errorf("marshal config: %w", err)
	}
	dir := filepath.Dir(configPath)
	if mkErr := os.MkdirAll(dir, 0755); mkErr != nil {
		c.mu.Unlock()
		return fmt.Errorf("create config dir: %w", mkErr)
	}
	writeErr := os.WriteFile(configPath, data, 0600)
	c.mu.Unlock() // release before notify so listeners are not blocked
	if writeErr != nil {
		return fmt.Errorf("write config %s: %w", configPath, writeErr)
	}
	if c.notify != nil {
		select {
		case c.notify <- true:
		default:
		}
	}
	return nil
}

// Save flushes the current config fields to disk under the config mutex.
// Prefer WithLockSave for any path that also mutates fields.
func (c *Config) Save() error {
	return c.WithLockSave(func(*Config) {})
}

// halConfigHashPath stores a hash of config.json captured when HAL was last
// (re)started.
const halConfigHashPath = "config/.hal_config_hash"

// hashConfigFile returns a hex SHA-256 of config.json's bytes, or "" if the
// file can't be read.
func hashConfigFile() string {
	data, err := os.ReadFile(configPath)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// HALConfigChanged reports whether config.json differs from the snapshot
// taken at the last HAL (re)start.
func HALConfigChanged() bool {
	current := hashConfigFile()
	if current == "" {
		return true
	}
	prev, err := os.ReadFile(halConfigHashPath)
	if err != nil {
		return true
	}
	return strings.TrimSpace(string(prev)) != current
}

// SnapshotHALConfig records config.json's current hash as the baseline for
// the running HAL process.
func SnapshotHALConfig() error {
	current := hashConfigFile()
	if current == "" {
		return fmt.Errorf("hash config: %s unreadable", configPath)
	}
	if err := os.MkdirAll(filepath.Dir(halConfigHashPath), 0755); err != nil {
		return fmt.Errorf("create config dir: %w", err)
	}
	if err := os.WriteFile(halConfigHashPath, []byte(current), 0600); err != nil {
		return fmt.Errorf("write %s: %w", halConfigHashPath, err)
	}
	return nil
}

// volumeStatePath persists the last speaker volume (0-100) set through HAL's
// /audio/volume endpoint.
const volumeStatePath = "config/.volume"

var hardwareProfileName = regexp.MustCompile(`^[a-z][a-z0-9_-]{0,63}$`)

// PersistedVolume returns the last volume (0-100) persisted by HAL, or
// (0, false) when none is valid yet.
func PersistedVolume() (int, bool) {
	return persistedVolume("/etc/autonomous/hardware-profile", volumeStatePath)
}

func persistedVolume(profilePath, statePath string) (int, bool) {
	profileData, err := os.ReadFile(profilePath)
	if err != nil && !os.IsNotExist(err) {
		return 0, false
	}
	profile := strings.TrimSpace(string(profileData))
	if profile != "" && profile != "standard" {
		if !hardwareProfileName.MatchString(profile) {
			return 0, false
		}
		statePath += "-" + profile
	}
	data, err := os.ReadFile(statePath)
	if err != nil {
		return 0, false
	}
	v, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil || v < 0 || v > 100 {
		return 0, false
	}
	return v, true
}

// SetLLMModel atomically sets LLMModel and saves the config in a single lock
// cycle (no gap between the field write and the marshal).
func (c *Config) SetLLMModel(key string) error {
	return c.WithLockSave(func(c *Config) {
		c.LLMModel = key
	})
}

// LLMModelKey returns LLMModel under the config mutex. Use this in goroutines
// that read LLMModel concurrently with WithLockSave paths.
func (c *Config) LLMModelKey() string {
	c.mu.Lock()
	key := c.LLMModel
	c.mu.Unlock()
	return key
}

// GetTTSAPIKey returns the TTS provider API key, falling back to LLMAPIKey
// when TTSAPIKey is unset so configs that pre-date the split keep working.
func (c *Config) GetTTSAPIKey() string {
	if c.TTSAPIKey != "" {
		return c.TTSAPIKey
	}
	return c.LLMAPIKey
}

// GetSTTAPIKey returns the AutonomousSTT API key, falling back to LLMAPIKey
// when STTAPIKey is unset.
func (c *Config) GetSTTAPIKey() string {
	if c.STTAPIKey != "" {
		return c.STTAPIKey
	}
	return c.LLMAPIKey
}

// GetSTTBaseURL returns the AutonomousSTT base URL, falling back to LLMBaseURL.
func (c *Config) GetSTTBaseURL() string {
	if c.STTBaseURL != "" {
		return c.STTBaseURL
	}
	return c.LLMBaseURL
}

// GetTTSBaseURL returns the TTS provider base URL, falling back to LLMBaseURL.
func (c *Config) GetTTSBaseURL() string {
	if c.TTSBaseURL != "" {
		return c.TTSBaseURL
	}
	return c.LLMBaseURL
}

// LocalIntentEnabled returns whether local intent matching is on (default true).
func (c *Config) LocalIntentEnabled() bool {
	if c.LocalIntent == nil {
		return true
	}
	return *c.LocalIntent
}

// LLMThinkingDisabled returns whether extended thinking is disabled (default false).
func (c *Config) LLMThinkingDisabled() bool {
	if c.LLMDisableThinking == nil {
		return false
	}
	return *c.LLMDisableThinking
}

// UserProfileReconcileEnabled reports whether the USER.md enrollment
// reconcile may WRITE (default true; false = observe only).
func (c *Config) UserProfileReconcileEnabled() bool {
	if c.UserProfileReconcile == nil {
		return true
	}
	return *c.UserProfileReconcile
}

// MemoryGuardEnabled reports whether the memory guard may WRITE.
func (c *Config) MemoryGuardEnabled() bool {
	if c.MemoryGuard == nil {
		return true
	}
	return *c.MemoryGuard
}

func (c *Config) GuardModeEnabled() bool {
	if c.GuardMode == nil {
		return false
	}
	return *c.GuardMode
}

// SensingTurnFloorSeconds returns the minimum gap in seconds between two
// ambient-sensing-initiated agent turns (default 120, 0 = disabled).
func (c *Config) SensingTurnFloorSeconds() int {
	if c.SensingTurnFloorS == nil {
		return 120
	}
	return *c.SensingTurnFloorS
}

func (c *Config) GetNotifyChannel() chan bool {
	return c.notify
}

func ProvideMQTTConfig(c *Config) mqtt.Config {
	return mqtt.Config{
		Endpoint: c.MQTTEndpoint,
		Username: c.MQTTUsername,
		Password: c.MQTTPassword,
		Port:     c.MQTTPort,
	}
}

// migrateOpenclawDir moves oldDir to newDir if oldDir exists and newDir does not.
func migrateOpenclawDir(oldDir, newDir string) error {
	if _, err := os.Stat(oldDir); os.IsNotExist(err) {
		return nil // nothing to migrate
	}
	if _, err := os.Stat(newDir); err == nil {
		return nil // destination already exists, skip
	}
	slog.Info("migrating openclaw config dir", "component", "config", "from", oldDir, "to", newDir)
	return os.Rename(oldDir, newDir)
}

// AutonomousDefaults is the shipped credential set, kept so an operator can
// get back to it after trying their own.
type AutonomousDefaults struct {
	BaseURL string `json:"base_url,omitempty" yaml:"baseURL"`
	APIKey  string `json:"api_key,omitempty" yaml:"apiKey"`
	Model   string `json:"model,omitempty" yaml:"model"`
}

// GetTTSSpeed prefers the saved rate, retaining legacy HAL_TTS_SPEED on
// upgrades.
func (c *Config) GetTTSSpeed() float64 {
	if c.TTSSpeed != nil {
		return *c.TTSSpeed
	}
	speed, err := strconv.ParseFloat(strings.TrimSpace(os.Getenv("HAL_TTS_SPEED")), 64)
	if err != nil || math.IsNaN(speed) || math.IsInf(speed, 0) {
		return 1.2
	}
	return math.Max(0.25, math.Min(4.0, speed))
}
