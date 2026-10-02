package domain

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

// Channel type identifiers. WhatsApp is add_channel only (interactive QR pairing), never a Setup channel.
const (
	ChannelTelegram = "telegram"
	ChannelSlack    = "slack"
	ChannelDiscord  = "discord"
	ChannelWhatsapp = "whatsapp"
	// ChannelIMessage is Apple iMessage via a user-hosted BlueBubbles server.
	ChannelIMessage = "imessage"
)

type SetupRequest struct {
	// Empty SSID = keep existing uplink (ethernet); empty Password = open network.
	SSID     string `json:"ssid"`
	Password string `json:"password"`

	// Channel is "telegram" (default), "slack", "discord" or "imessage".
	Channel string `json:"channel"`

	TelegramBotToken string `json:"telegram_bot_token"`
	TelegramUserID   string `json:"telegram_user_id"`

	SlackBotToken string `json:"slack_bot_token"`
	SlackAppToken string `json:"slack_app_token"`
	SlackUserID   string `json:"slack_user_id"`

	DiscordBotToken string `json:"discord_bot_token"`
	DiscordGuildID  string `json:"discord_guild_id"`
	DiscordUserID   string `json:"discord_user_id"`

	// BlueBubbles server URL, password, and the operator's iMessage handle.
	BluebubblesServerURL   string `json:"bluebubbles_server_url"`
	BluebubblesPassword    string `json:"bluebubbles_password"`
	BluebubblesUserAddress string `json:"bluebubbles_user_address"`

	LLMBaseURL string `json:"llm_base_url" validate:"required"`
	LLMAPIKey  string `json:"llm_api_key" validate:"required"`
	LLMModel   string `json:"llm_model"`

	DeepgramAPIKey string `json:"deepgram_api_key"`
	// STT/TTS keys and base URLs override the LLM ones; empty = fall back.
	STTAPIKey   string `json:"stt_api_key"`
	TTSAPIKey   string `json:"tts_api_key"`
	STTBaseURL  string `json:"stt_base_url"`
	TTSBaseURL  string `json:"tts_base_url"`
	STTLanguage string `json:"stt_language"`
	TTSProvider string `json:"tts_provider"`
	TTSVoice    string `json:"tts_voice"`

	DeviceID string `json:"device_id" validate:"required"`

	// AdminPassword is plaintext; only its bcrypt hash is persisted.
	AdminPassword string `json:"admin_password"`

	// Empty MQTTEndpoint disables MQTT.
	MQTTEndpoint string `json:"mqtt_endpoint"`
	MQTTUsername string `json:"mqtt_username"`
	MQTTPassword string `json:"mqtt_password"`
	MQTTPort     int    `json:"mqtt_port"`
	FAChannel    string `json:"fa_channel"`
	FDChannel    string `json:"fd_channel"`

	LLMDisableThinking *bool `json:"llm_disable_thinking,omitempty"`
}

// WifiProvisionRequest is the payload for POST /api/device/wifi-provision (AP-portal re-provision).
// Only SSID is required; empty fields keep the on-disk value.
type WifiProvisionRequest struct {
	SSID     string `json:"ssid" validate:"required"`
	Password string `json:"password"` // empty = open network

	LLMAPIKey  string `json:"llm_api_key"`
	LLMBaseURL string `json:"llm_base_url"`
	LLMModel   string `json:"llm_model"`

	DeepgramAPIKey string `json:"deepgram_api_key"`
	STTAPIKey      string `json:"stt_api_key"`
	STTBaseURL     string `json:"stt_base_url"`
	STTLanguage    string `json:"stt_language"`
	TTSAPIKey      string `json:"tts_api_key"`
	TTSBaseURL     string `json:"tts_base_url"`
	TTSProvider    string `json:"tts_provider"`
	TTSVoice       string `json:"tts_voice"`

	// AdminPassword is plaintext; empty keeps the current hash.
	AdminPassword string `json:"admin_password"`

	// Empty Channel keeps the on-disk value; token fields apply only to the matching Channel.
	Channel                string `json:"channel"`
	TelegramBotToken       string `json:"telegram_bot_token"`
	TelegramUserID         string `json:"telegram_user_id"`
	SlackBotToken          string `json:"slack_bot_token"`
	SlackAppToken          string `json:"slack_app_token"`
	SlackUserID            string `json:"slack_user_id"`
	DiscordBotToken        string `json:"discord_bot_token"`
	DiscordGuildID         string `json:"discord_guild_id"`
	DiscordUserID          string `json:"discord_user_id"`
	BluebubblesServerURL   string `json:"bluebubbles_server_url"`
	BluebubblesPassword    string `json:"bluebubbles_password"`
	BluebubblesUserAddress string `json:"bluebubbles_user_address"`
}

// EffectiveChannel returns the resolved channel type, defaulting to "telegram" (also for whatsapp).
func (r *SetupRequest) EffectiveChannel() string {
	if r.Channel == ChannelSlack {
		return ChannelSlack
	}
	if r.Channel == ChannelDiscord {
		return ChannelDiscord
	}
	if r.Channel == ChannelIMessage {
		return ChannelIMessage
	}
	return ChannelTelegram
}

// ValidateChannel checks that the required fields for the selected channel are present.
func (r *SetupRequest) ValidateChannel() error {
	switch r.EffectiveChannel() {
	case "slack":
		if r.SlackBotToken == "" {
			return fmt.Errorf("slack_bot_token is required for slack channel")
		}
		if r.SlackAppToken == "" {
			return fmt.Errorf("slack_app_token is required for slack channel")
		}
	case "discord":
		if r.DiscordBotToken == "" {
			return fmt.Errorf("discord_bot_token is required for discord channel")
		}
		if r.DiscordGuildID == "" {
			return fmt.Errorf("discord_guild_id is required for discord channel")
		}
		if r.DiscordUserID == "" {
			return fmt.Errorf("discord_user_id is required for discord channel")
		}
	case "imessage":
		if r.BluebubblesServerURL == "" {
			return fmt.Errorf("bluebubbles_server_url is required for imessage channel")
		}
		if r.BluebubblesPassword == "" {
			return fmt.Errorf("bluebubbles_password is required for imessage channel")
		}
		if r.BluebubblesUserAddress == "" {
			return fmt.Errorf("bluebubbles_user_address is required for imessage channel")
		}
	default:
		if r.TelegramBotToken == "" {
			return fmt.Errorf("telegram_bot_token is required for telegram channel")
		}
		if r.TelegramUserID == "" {
			return fmt.Errorf("telegram_user_id is required for telegram channel")
		}
	}
	return nil
}

// AddChannelRequest is used to add a messaging channel after initial setup.
type AddChannelRequest struct {
	// Channel is "telegram", "slack", "discord", "whatsapp" or "imessage".
	Channel string `json:"channel" validate:"required"`

	TelegramBotToken string `json:"telegram_bot_token"`
	TelegramUserID   string `json:"telegram_user_id"`

	SlackBotToken string `json:"slack_bot_token"`
	SlackAppToken string `json:"slack_app_token"`
	SlackUserID   string `json:"slack_user_id"`
	// SlackMode is "socket" (default, needs SlackAppToken) or "http" (proxy POSTs, needs SlackSigningSecret).
	SlackMode          string `json:"slack_mode,omitempty"`
	SlackSigningSecret string `json:"slack_signing_secret,omitempty"`
	SlackWebhookPath   string `json:"slack_webhook_path,omitempty"` // default /slack/events in http mode

	DiscordBotToken string `json:"discord_bot_token"`
	DiscordGuildID  string `json:"discord_guild_id"`
	DiscordUserID   string `json:"discord_user_id"`

	// WhatsappUserID is the operator's E.164 phone number (permitted DM caller).
	WhatsappUserID string `json:"whatsapp_user_id"`

	BluebubblesServerURL   string `json:"bluebubbles_server_url"`
	BluebubblesPassword    string `json:"bluebubbles_password"`
	BluebubblesUserAddress string `json:"bluebubbles_user_address"`
	// BluebubblesCallerContext is optional text prepended to incoming iMessages; empty = plugin default.
	BluebubblesCallerContext string `json:"bluebubbles_caller_context"`
}

// RefreshChannelRequest carries credentials (read from config.json, never MQTT) for RefreshChannelConfig.
type RefreshChannelRequest struct {
	Channel string

	TelegramBotToken string
	TelegramUserID   string

	SlackBotToken string
	SlackAppToken string
	SlackUserID   string
	// SlackMode: see AddChannelRequest.SlackMode.
	SlackMode          string
	SlackSigningSecret string
	SlackWebhookPath   string

	DiscordBotToken string
	DiscordGuildID  string
	DiscordUserID   string

	BluebubblesServerURL     string
	BluebubblesPassword      string
	BluebubblesUserAddress   string
	BluebubblesCallerContext string
}

// EffectiveSlackMode resolves SlackMode, defaulting to "socket".
func (r *AddChannelRequest) EffectiveSlackMode() string {
	if r.SlackMode == "http" {
		return "http"
	}
	return "socket"
}

// EffectiveChannel returns the resolved channel type, defaulting to "telegram".
func (r *AddChannelRequest) EffectiveChannel() string {
	switch r.Channel {
	case ChannelSlack:
		return ChannelSlack
	case ChannelDiscord:
		return ChannelDiscord
	case ChannelWhatsapp:
		return ChannelWhatsapp
	case ChannelIMessage:
		return ChannelIMessage
	}
	return ChannelTelegram
}

// ValidateChannel checks that the required fields for the selected channel are present.
func (r *AddChannelRequest) ValidateChannel() error {
	switch r.EffectiveChannel() {
	case ChannelSlack:
		if r.SlackBotToken == "" {
			return fmt.Errorf("slack_bot_token is required for slack channel")
		}
		switch r.EffectiveSlackMode() {
		case "http":
			if r.SlackSigningSecret == "" {
				return fmt.Errorf("slack_signing_secret is required for slack channel in http mode")
			}
		default:
			if r.SlackAppToken == "" {
				return fmt.Errorf("slack_app_token is required for slack channel in socket mode")
			}
		}
	case ChannelDiscord:
		if r.DiscordBotToken == "" {
			return fmt.Errorf("discord_bot_token is required for discord channel")
		}
		if r.DiscordGuildID == "" {
			return fmt.Errorf("discord_guild_id is required for discord channel")
		}
		if r.DiscordUserID == "" {
			return fmt.Errorf("discord_user_id is required for discord channel")
		}
	case ChannelWhatsapp:
		if r.WhatsappUserID == "" {
			return fmt.Errorf("whatsapp_user_id is required for whatsapp channel")
		}
	case ChannelIMessage:
		if r.BluebubblesServerURL == "" {
			return fmt.Errorf("bluebubbles_server_url is required for imessage channel")
		}
		if r.BluebubblesPassword == "" {
			return fmt.Errorf("bluebubbles_password is required for imessage channel")
		}
		if r.BluebubblesUserAddress == "" {
			return fmt.Errorf("bluebubbles_user_address is required for imessage channel")
		}
	default:
		if r.TelegramBotToken == "" {
			return fmt.Errorf("telegram_bot_token is required for telegram channel")
		}
		if r.TelegramUserID == "" {
			return fmt.Errorf("telegram_user_id is required for telegram channel")
		}
	}
	return nil
}

type SetupResponse struct {
	Success bool `json:"success"`
}

// Command types received from server via MQTT FAChannel.
const (
	CommandInfo         = "info"
	CommandAddChannel   = "add_channel"
	CommandOTA          = "ota"
	CommandData         = "data"
	CommandWhatsappPair = "whatsapp_pair"

	// CommandClaudeCodeLogin starts the claude.ai OAuth login (ClaudeLoginPairer); the code returns via CommandClaudeCodeLoginCode.
	CommandClaudeCodeLogin     = "claudecode_login"
	CommandClaudeCodeLoginCode = "claudecode_login_code"

	// CommandSlackEvent forwards a verbatim Slack Events API POST to the local gateway, which verifies the signature.
	// Only relevant for devices in slack_mode="http".
	CommandSlackEvent = "slack_event"

	// CommandSlackCommand forwards a Slack slash command like CommandSlackEvent; event_id carries trigger_id.
	CommandSlackCommand = "slack_command"
)

// Data kinds carried inside CommandData envelope.
const (
	KindBuddyPairStart      = "buddy.pair.start" // 6-digit Buddy pairing code, valid 60s
	KindBuddyStatus         = "buddy.status"
	KindBuddyPairRevoke     = "buddy.pair.revoke"
	KindHarnessPairStart    = "harness.pair.start"
	KindHarnessStatus       = "harness.status"
	KindHarnessVoiceModeGet = "harness.voice-mode.get"
	KindHarnessVoiceModeSet = "harness.voice-mode.set"
	KindHarnessPairCancel   = "harness.pair.cancel"
	KindHarnessPairRevoke   = "harness.pair.revoke"

	KindTTSSet       = "tts.set"
	KindTTSPreview   = "tts.preview" // no config write
	KindDeviceRename = "device.rename"
	KindOAuthSet     = "oauth.set"
	KindOAuthRemove  = "oauth.remove"
	KindRealtimeSet  = "realtime.set"
	// KindRealtimeGet reads the realtime settings and the valid provider/voice/reasoning options.
	KindRealtimeGet  = "realtime.get"
	KindWakeWordGate = "wakeword.gate"
	KindTimezoneSet  = "timezone.set"
	// KindDeviceSoftReset wipes config.json and restarts os-server into AP setup mode (no reboot).
	KindDeviceSoftReset = "device.soft_reset"

	// <runtime>.setup kinds switch the active agentic backend; the kind names the target runtime.
	KindHermesSetup     = "hermes.setup"
	KindPicoclawSetup   = "picoclaw.setup"
	KindClaudecodeSetup = "claudecode.setup"
	KindOpenclawSetup   = "openclaw.setup"
	KindCodexSetup      = "codex.setup"
	KindOpenCodeSetup   = "opencode.setup"

	// Swappable agentic backends; must match agent/factory.go and switch-runtime.
	AgentRuntimeOpenClaw   = "openclaw"
	AgentRuntimeHermes     = "hermes"
	AgentRuntimePicoclaw   = "picoclaw"
	AgentRuntimeCodex      = "codex"
	AgentRuntimeClaudeCode = "claudecode"
	AgentRuntimeOpenCode   = "opencode"
	// AgentRuntimeRemote uses a brain on another machine; the URL/token are saved but no switch happens yet.
	AgentRuntimeRemote = "remote"

	KindSystemInfo     = "system.info"
	KindSystemVersion  = "system.version"
	KindSystemNetwork  = "system.network"
	KindSystemReboot   = "system.reboot"   // via HAL
	KindSystemShutdown = "system.shutdown" // via HAL (servo-aware)

	// KindSystemOTAVersions reports per-component current vs published versions.
	KindSystemOTAVersions = "system.ota_versions"
	// KindSystemSoftwareUpdate force-updates one component; acks on start, then reports completion.
	KindSystemSoftwareUpdate = "system.software_update"

	// KindSkillsInstall installs a role's skill bundle. Data: {"role":"<role>"}.
	KindSkillsInstall = "skills.install"

	// KindSkillsSave writes one authored skill (MQTT twin of POST /api/agent/skills).
	KindSkillsSave = "skills.save"

	// KindSkillsUpload installs an inline .md, .zip or .skill file.
	KindSkillsUpload = "skills.upload"

	// KindSkillsInstallStore installs one catalog skill by id.
	KindSkillsInstallStore = "skills.install_store"

	// KindSkillsFiles returns a skill's file list, or one file's text when `path` is set.
	KindSkillsFiles = "skills.files"

	// KindSkillsUninstall removes one installed skill.
	KindSkillsUninstall = "skills.uninstall"

	// KindChannelRefreshConfig re-applies channels.<channel> config from on-device credentials.
	KindChannelRefreshConfig = "channel.refresh_config"

	// KindAddChannel is the data-envelope twin of CommandAddChannel, usable with the privacy envelope.
	KindAddChannel = "add_channel"

	// KindChatSend starts an agent turn from the backend; the run streams back as chat.event messages.
	KindChatSend = "chat.send"

	// KindChatEvent is device-initiated: one MonitorEvent of a chat.send run.
	KindChatEvent = "chat.event"

	// KindChatFileGet fetches one device-local file named in a turn.
	// The client-supplied path is gated by the system/agentfile allow-list.
	KindChatFileGet = "chat.file.get"

	// KindScheduleSync replaces the full scheduled-task list (backend is authoritative).
	KindScheduleSync = "schedule.sync"

	// KindScheduleRun runs one stored schedule now without changing its cadence.
	KindScheduleRun = "schedule.run"

	// KindScheduleMutate is an outbound schedule-change proposal; the next schedule.sync is the truth.
	KindScheduleMutate = "schedule.mutate"

	// KindScheduleMutateAck is the backend's terminal verdict on one proposal; the device stops retrying it.
	KindScheduleMutateAck = "schedule.mutate.ack"

	// KindFaceEnroll enrolls one face photo via HAL (MQTT twin of HAL POST /face/enroll).
	KindFaceEnroll = "face.enroll"

	// KindFaceOwners lists enrolled people via HAL GET /face/owners.
	KindFaceOwners = "face.owners"

	// KindFaceRemove deletes one person's whole users/<label>/ folder via HAL POST /face/remove.
	KindFaceRemove = "face.remove"

	// KindVoiceEnroll records from the lamp's own mic and enrolls the voice via HAL POST /speaker/record-enroll.
	KindVoiceEnroll = "voice.enroll"

	// KindVoiceOwners lists everyone's voice sample files from users/<label>/voice/.
	KindVoiceOwners = "voice.owners"

	// KindVoiceRemove deletes one person's whole voice profile via HAL POST /speaker/remove.
	KindVoiceRemove = "voice.remove"

	// KindVoiceFileGet returns one voice sample file inline (base64) for playback.
	KindVoiceFileGet = "voice.file.get"

	// KindVoiceFileRemove deletes one voice sample and its embedding (same as POST /api/voice/file/remove).
	KindVoiceFileRemove = "voice.file.remove"

	// KindLEDRestingGet reads the owner's resting light choice via HAL GET /led/resting.
	KindLEDRestingGet = "led.resting.get"

	// KindLEDRestingSet saves the resting light (default, off, or custom colour) via HAL PUT /led/resting.
	KindLEDRestingSet = "led.resting.set"

	// KindLEDRestingPreview shows a candidate colour without saving it via HAL POST /led/resting/preview.
	KindLEDRestingPreview = "led.resting.preview"

	// KindVolumeGet reads the speaker volume as a share of the device's allowed range.
	KindVolumeGet = "volume.get"

	// KindVolumeSet sets the speaker volume (0-100% of the allowed range) via HAL POST /audio/volume.
	KindVolumeSet = "volume.set"

	// KindMicGet reads the mic mute state and the hardware mic switch.
	KindMicGet = "mic.get"

	// KindMicSet mutes or unmutes the mic via HAL POST /voice/mute | /voice/unmute.
	KindMicSet = "mic.set"
)

// Connector (MCP) data-kind prefixes; the connector code is the suffix (e.g. "connector.set.notion").
const (
	DataKindConnectorSetPrefix    = "connector.set."
	DataKindConnectorRemovePrefix = "connector.remove."
)

// MQTTMessage is the standard envelope for MQTT messages from the server (fa_channel).
type MQTTMessage struct {
	Cmd     string          `json:"cmd"`
	Kind    string          `json:"kind"`
	RawData json.RawMessage `json:"-"`
	raw     []byte
}

// UnmarshalJSON custom unmarshals to keep the full raw payload accessible to handlers.
func (m *MQTTMessage) UnmarshalJSON(data []byte) error {
	type alias struct {
		Cmd  string `json:"cmd"`
		Kind string `json:"kind"`
	}
	var a alias
	if err := json.Unmarshal(data, &a); err != nil {
		return err
	}
	m.Cmd = a.Cmd
	m.Kind = a.Kind
	m.raw = make([]byte, len(data))
	copy(m.raw, data)
	return nil
}

// Raw returns the full original JSON payload for handlers to parse additional fields.
func (m *MQTTMessage) Raw() []byte {
	return m.raw
}

type MQTTAddChannelRequest struct {
	Channel string                 `json:"channel" validate:"required"`
	Config  map[string]interface{} `json:"config"`
}

// MQTTAddChannelCommand is the fa_channel payload for cmd:"add_channel".
type MQTTAddChannelCommand struct {
	Channel string                 `json:"channel"`
	Config  map[string]interface{} `json:"config"`
}

// firstNonEmptyString returns the first non-empty string value among keys in cfg.
func firstNonEmptyString(cfg map[string]interface{}, keys ...string) string {
	for _, k := range keys {
		if v, ok := cfg[k].(string); ok && v != "" {
			return v
		}
	}
	return ""
}

func (r *MQTTAddChannelCommand) ToRequest() AddChannelRequest {
	var req AddChannelRequest
	req.Channel = r.Channel
	cfg := r.Config
	switch r.Channel {
	case ChannelDiscord:
		req.DiscordBotToken, _ = cfg["bot_token"].(string)
		req.DiscordGuildID, _ = cfg["guild_id"].(string)
		req.DiscordUserID, _ = cfg["user_id"].(string)
	case ChannelSlack:
		req.SlackBotToken, _ = cfg["bot_token"].(string)
		req.SlackAppToken, _ = cfg["app_token"].(string)
		req.SlackUserID, _ = cfg["channel_id"].(string)
		req.SlackMode, _ = cfg["mode"].(string)
		req.SlackSigningSecret, _ = cfg["signing_secret"].(string)
		req.SlackWebhookPath, _ = cfg["webhook_path"].(string)
	case ChannelWhatsapp:
		req.WhatsappUserID, _ = cfg["user_id"].(string)
	case ChannelIMessage:
		req.BluebubblesServerURL = firstNonEmptyString(cfg, "server_url", "bluebubbles_server_url")
		req.BluebubblesPassword = firstNonEmptyString(cfg, "password", "bluebubbles_password")
		req.BluebubblesUserAddress = firstNonEmptyString(cfg, "user_address", "bluebubbles_user_address")
		req.BluebubblesCallerContext = firstNonEmptyString(cfg, "caller_context", "bluebubbles_caller_context")
	default:
		req.TelegramBotToken, _ = cfg["bot_token"].(string)
		req.TelegramUserID, _ = cfg["chat_id"].(string)
	}
	return req
}

// MQTTAddChannelResponse extends MQTTInfoResponse with channel fields; WhatsApp streams pairing statuses.
// PairingQR* fields are set only when Status="pairing_qr".
type MQTTAddChannelResponse struct {
	MQTTInfoResponse
	Channel          string `json:"channel"`
	Status           string `json:"status"`
	Error            string `json:"error,omitempty"`
	PairingQRText    string `json:"pairing_qr_text,omitempty"`
	PairingQRFormat  string `json:"pairing_qr_format,omitempty"`
	PairingQRSeq     int    `json:"pairing_qr_seq,omitempty"`
	PairingExpiresAt string `json:"pairing_expires_at,omitempty"`
}

// MQTTWhatsappPairCommand is the fa_channel payload for cmd:"whatsapp_pair".
type MQTTWhatsappPairCommand struct{}

// MQTTWhatsappPairResponse mirrors MQTTAddChannelResponse for re-pair flows.
type MQTTWhatsappPairResponse struct {
	MQTTInfoResponse
	Status           string `json:"status"`
	Error            string `json:"error,omitempty"`
	PairingQRText    string `json:"pairing_qr_text,omitempty"`
	PairingQRFormat  string `json:"pairing_qr_format,omitempty"`
	PairingQRSeq     int    `json:"pairing_qr_seq,omitempty"`
	PairingExpiresAt string `json:"pairing_expires_at,omitempty"`
}

// MQTTClaudeCodeLoginCodeCommand carries the OAuth code for cmd:"claudecode_login_code".
type MQTTClaudeCodeLoginCodeCommand struct {
	Code string `json:"code"`
}

// MQTTClaudeCodeLoginResponse streams claude.ai login statuses and acks login-code submissions.
type MQTTClaudeCodeLoginResponse struct {
	MQTTInfoResponse
	Status   string `json:"status"`
	Error    string `json:"error,omitempty"`
	LoginURL string `json:"login_url,omitempty"`
}

type MQTTRemoveChannelRequest struct {
	Channel string `json:"channel" validate:"required"`
}

type MQTTRemoveChannelResponse struct {
	Success bool `json:"success"`
}

// MQTTInfoResponse is the base response published to fd_channel; all messages include it.
type MQTTInfoResponse struct {
	Device      string  `json:"device"`
	Type        string  `json:"type"`
	Version     string  `json:"version"`
	ID          string  `json:"id"`
	Mac         string  `json:"mac"`
	Time        string  `json:"time"`
	TTSProvider string  `json:"tts_provider,omitempty"`
	TTSVoice    string  `json:"tts_voice,omitempty"`
	TTSSpeed    float64 `json:"tts_speed"`
	STTLanguage string  `json:"stt_language,omitempty"`
	// WakeWordEnabled is never omitted so disabled differs from an older device not reporting it.
	WakeWordEnabled bool `json:"wakeword_enabled"`
	// Timezone is the active IANA zone (e.g. "Asia/Ho_Chi_Minh").
	Timezone          string `json:"timezone,omitempty"`
	HalVersion        string `json:"hal_version,omitempty"`
	OpenClawVersion   string `json:"openclaw_version,omitempty"`
	HermesVersion     string `json:"hermes_version,omitempty"`
	PicoclawVersion   string `json:"picoclaw_version,omitempty"`
	CodexVersion      string `json:"codex_version,omitempty"`
	ClaudeCodeVersion string `json:"claudecode_version,omitempty"`
	OpenCodeVersion   string `json:"opencode_version,omitempty"`
	AgentRuntime      string `json:"agent_runtime,omitempty"`
	LocalIP           string `json:"local_ip,omitempty"`
	// UnsupportedChannels lists configured channels the active runtime cannot run.
	UnsupportedChannels []string `json:"unsupported_channels,omitempty"`
	// Skills lists the active runtime's installed skills; set only by handleInfo.
	Skills []SkillSummary `json:"skills,omitempty"`
	// SchedulesDigest fingerprints stored schedules (schedule.Digest); a mismatch triggers a full schedule.sync.
	// Set only by handleInfo and omitted when the store is unreadable.
	SchedulesDigest string `json:"schedules_digest,omitempty"`
}

// NewMQTTInfoResponse creates a base message with required fields populated from config.
func NewMQTTInfoResponse(cfg *config.Config, msgType string, mac string) MQTTInfoResponse {
	return MQTTInfoResponse{
		Device:          cfg.DeviceTypeOrDefault(),
		Type:            msgType,
		Version:         config.OSVersion,
		ID:              cfg.DeviceID,
		Mac:             mac,
		Time:            time.Now().UTC().Format(time.RFC3339Nano),
		TTSProvider:     cfg.TTSProvider,
		TTSVoice:        cfg.TTSVoice,
		TTSSpeed:        cfg.GetTTSSpeed(),
		STTLanguage:     cfg.STTLanguage,
		WakeWordEnabled: cfg.WakeWordEnabled(),
		Timezone:        cfg.Timezone,
	}
}

// KindEnvironmentStatus queries the current model-independent HAL snapshot.
const KindEnvironmentStatus = "environment.status"

// MQTTDataCommand is the generic fa_channel envelope for cmd:"data", dispatched by Kind.
// Type "privacy" means Data is fetched from the backend over TLS instead of read inline.
type MQTTDataCommand struct {
	Kind string          `json:"kind"`
	Type string          `json:"type,omitempty"`
	Data json.RawMessage `json:"data"`
	// Channel optionally narrows the privacy fetch for channel-keyed kinds.
	Channel string `json:"channel,omitempty"`
}

// MQTT data delivery types and statuses for the privacy envelope flow.
const (
	// MQTTDataTypePrivacy marks Data fetched from the backend, keeping secrets off the broker.
	MQTTDataTypePrivacy = "privacy"
	// MQTTStatusReceived is the non-terminal ack for an accepted privacy envelope.
	MQTTStatusReceived = "received"
)

// MQTTDataResponse is the fd_channel reply for cmd:"data"; Kind is echoed for correlation.
type MQTTDataResponse struct {
	MQTTInfoResponse
	Kind   string      `json:"kind"`
	Status string      `json:"status"`
	Error  string      `json:"error,omitempty"`
	Data   interface{} `json:"data,omitempty"`
}

// MQTTSystemInfoData is the kind:"system.info" payload; fields are zero-valued when a probe fails.
type MQTTSystemInfoData struct {
	Versions MQTTVersionsData `json:"versions"`
	Network  MQTTNetworkData  `json:"network"`
	Host     MQTTHostData     `json:"host"`
}

// MQTTVersionsData carries component versions; empty means probing failed.
type MQTTVersionsData struct {
	OSServer         string `json:"os-server"`
	Bootstrap        string `json:"bootstrap"`
	Hal              string `json:"hal"`
	OpenClaw         string `json:"openclaw"`
	OpenClawDetected bool   `json:"openclaw_detected"`
}

// MQTTNetworkData carries link facts for the default-route interface; SSID is empty when not on Wi-Fi.
type MQTTNetworkData struct {
	PrivateIP string `json:"private_ip"`
	Interface string `json:"interface"`
	MAC       string `json:"mac"`
	SSID      string `json:"ssid"`
	Gateway   string `json:"gateway"`
}

// MQTTHostData carries host-process facts useful for ops dashboards.
type MQTTHostData struct {
	Hostname      string `json:"hostname"`
	DeviceID      string `json:"device_id"`
	DeviceName    string `json:"device_name"` // friendly "<device_type>-xxxx"
	UptimeSeconds int64  `json:"uptime_seconds"`
	Timezone      string `json:"timezone,omitempty"` // active IANA zone
}

// MQTTOAuthSetData is the Data payload for kind:"oauth.set"; Provider keys access_tokens.json.
type MQTTOAuthSetData struct {
	Provider     string   `json:"provider"`
	AccessToken  string   `json:"access_token"`
	RefreshToken string   `json:"refresh_token,omitempty"`
	TokenType    string   `json:"token_type,omitempty"`
	ExpiresAt    int64    `json:"expires_at,omitempty"` // unix seconds; 0 = never expires
	Scopes       []string `json:"scopes,omitempty"`
	UserEmail    string   `json:"user_email,omitempty"`
	ClientID     string   `json:"client_id,omitempty"`
}

// MQTTOAuthRemoveData is the Data payload for kind:"oauth.remove".
type MQTTOAuthRemoveData struct {
	Provider string `json:"provider"`
}

// MQTTChannelRefreshConfigData is the Data payload for kind:"channel.refresh_config".
type MQTTChannelRefreshConfigData struct {
	Channel string `json:"channel"`
}

// MQTTChannelRefreshConfigResultData is the channel.refresh_config result; Runtime is the detected version.
type MQTTChannelRefreshConfigResultData struct {
	Channel string `json:"channel"`
	Runtime string `json:"runtime,omitempty"`
}

// OAuthTokenEntry is one provider's token inside access_tokens.json.
type OAuthTokenEntry struct {
	AccessToken    string   `json:"access_token"`
	RefreshToken   string   `json:"refresh_token,omitempty"`
	TokenType      string   `json:"token_type,omitempty"`
	ExpiresAt      int64    `json:"expires_at,omitempty"`
	Scopes         []string `json:"scopes,omitempty"`
	UserEmail      string   `json:"user_email,omitempty"`
	ClientID       string   `json:"client_id,omitempty"`
	ObtainedAt     int64    `json:"obtained_at"`               // unix seconds
	RefreshRevoked bool     `json:"refresh_revoked,omitempty"` // invalid_grant seen; skip until re-auth
}

// AccessTokensFile is the on-disk schema for workspace/configs/access_tokens.json.
type AccessTokensFile struct {
	Version   int                        `json:"version"`
	Providers map[string]OAuthTokenEntry `json:"providers"`
}

// MQTTConnectorSetData is the Data payload for kind:"connector.set.<code>".
type MQTTConnectorSetData struct {
	Connector    string `json:"connector"`
	AuthType     string `json:"auth_type"`
	AccessToken  string `json:"access_token,omitempty"`
	RefreshToken string `json:"refresh_token,omitempty"`
	TokenType    string `json:"token_type,omitempty"`
	ExpiresIn    int    `json:"expires_in,omitempty"` // seconds from now
	ExpiresAt    int64  `json:"expires_at,omitempty"` // unix seconds (wins over expires_in)
	// APIKey is used by static-API-key (non-OAuth) connectors.
	APIKey    string   `json:"api_key,omitempty"`
	Scopes    []string `json:"scopes,omitempty"`
	UserEmail string   `json:"user_email,omitempty"`
	ClientID  string   `json:"client_id,omitempty"`
	// Credentials holds connector-specific extras, preserved across refreshes.
	Credentials map[string]string `json:"credentials,omitempty"`
	// Refresh enables auto-rotation (also requires a refresh_token).
	Refresh bool `json:"refresh,omitempty"`
}

// MQTTConnectorRemoveData is the Data payload for kind:"connector.remove.<code>".
type MQTTConnectorRemoveData struct {
	Connector string `json:"connector"`
}

// ConnectorEntry is one connector's credentials inside workspace/configs/connectors.json.
type ConnectorEntry struct {
	AuthType     string            `json:"auth_type,omitempty"`
	AccessToken  string            `json:"access_token"`
	RefreshToken string            `json:"refresh_token,omitempty"`
	TokenType    string            `json:"token_type,omitempty"`
	ExpiresAt    int64             `json:"expires_at,omitempty"`
	APIKey       string            `json:"api_key,omitempty"`
	Scopes       []string          `json:"scopes,omitempty"`
	UserEmail    string            `json:"user_email,omitempty"`
	ClientID     string            `json:"client_id,omitempty"`
	Credentials  map[string]string `json:"credentials,omitempty"`
	Refresh      bool              `json:"refresh,omitempty"`
	ObtainedAt   int64             `json:"obtained_at"`
}

// ConnectorsFile is the on-disk schema for workspace/configs/connectors.json.
type ConnectorsFile struct {
	Version    int                       `json:"version"`
	Connectors map[string]ConnectorEntry `json:"connectors"`
}

// MQTTSkillsInstallData is the Data payload for kind:"skills.install" (role bundle slug).
type MQTTSkillsInstallData struct {
	Role string `json:"role"`
}

// MQTTSoftwareUpdateData is the system.software_update data; Target "agent" means the active runtime's CLI.
type MQTTSoftwareUpdateData struct {
	Target string `json:"target"`
}

// MQTTSkillsSaveData is the Data payload for kind:"skills.save"; Name must match ^[a-z0-9_-]+$.
type MQTTSkillsSaveData struct {
	Name         string `json:"name"`
	Description  string `json:"description"`
	Instructions string `json:"instructions"`
}

// MQTTSkillsUploadData is the Data payload for kind:"skills.upload" (capped at skills.StoreMaxBytes).
type MQTTSkillsUploadData struct {
	Filename      string `json:"filename"`
	ContentBase64 string `json:"content_base64"`
}

// MQTTSkillsFilesData is the Data payload for kind:"skills.files"; Path is as the file list reported it.
type MQTTSkillsFilesData struct {
	Name string `json:"name"`
	Path string `json:"path"`
}

// InboundFile is a user-attached non-image file for a turn (images use the vision path).
type InboundFile struct {
	// Name is used only for its extension; the on-disk path is generated.
	Name string `json:"name"`
	// MIME is advisory only.
	MIME string `json:"mime,omitempty"`
	// Content is the file base64-encoded, capped at agentfile.InboundMaxBytes.
	Content string `json:"content"`
}

// MQTTChatSendData is the Data payload for kind:"chat.send".
type MQTTChatSendData struct {
	Message string        `json:"message"`
	Files   []InboundFile `json:"files,omitempty"`
	// Images are optional base64 JPEGs.
	Images []string `json:"images,omitempty"`
	// SessionID is opaque and echoed back; it does not partition conversation state.
	SessionID string `json:"session_id,omitempty"`
	// Speak also plays the reply aloud (off by default).
	Speak bool `json:"speak,omitempty"`
}

// MQTTChatSendResult is the Data block of the chat.send ack.
type MQTTChatSendResult struct {
	RunID     string `json:"run_id"`
	SessionID string `json:"session_id,omitempty"`
}

// MQTTChatEventData is the Data payload for kind:"chat.event".
type MQTTChatEventData struct {
	RunID     string       `json:"run_id"`
	SessionID string       `json:"session_id,omitempty"`
	Event     MonitorEvent `json:"event"`
}

// MQTTChatFileGetData is the Data payload for kind:"chat.file.get".
type MQTTChatFileGetData struct {
	// Path is untrusted and validated against the agentfile allow-list.
	Path string `json:"path"`
	// SessionID and RunID are optional and echoed back untouched.
	SessionID string `json:"session_id,omitempty"`
	RunID     string `json:"run_id,omitempty"`
}

// MQTTChatFileData is the chat.file.get reply: metadata plus bytes when they fit.
type MQTTChatFileData struct {
	RunID     string `json:"run_id,omitempty"`
	SessionID string `json:"session_id,omitempty"`
	// Name is the basename; Path echoes the request.
	Name string `json:"name"`
	Path string `json:"path"`
	MIME string `json:"mime"`
	Size int64  `json:"size"`
	// Content is base64; empty when TooLarge is set.
	Content string `json:"content,omitempty"`
	// TooLarge marks a file past the MQTT inline budget (metadata only).
	TooLarge bool `json:"too_large,omitempty"`
}

// MQTTSkillsUninstallData is the Data payload for kind:"skills.uninstall".
type MQTTSkillsUninstallData struct {
	Name string `json:"name"`
}

// MQTTSkillsInstallStoreData is the Data payload for kind:"skills.install_store".
type MQTTSkillsInstallStoreData struct {
	ID string `json:"id"`
	// Name is a fallback used when the archive has no single wrapping directory.
	Name string `json:"name"`
}

// MQTTTTSSetData is the data payload for kind:"tts.set" downlinks.
type MQTTTTSSetData struct {
	Speed    *float64 `json:"speed,omitempty"`
	Provider string   `json:"provider"`
	Voice    string   `json:"voice"`
	Language string   `json:"language"`
}

// MQTTTTSSetCommand wraps the full tts.set downlink envelope for unmarshalling.
type MQTTTTSSetCommand struct {
	Data MQTTTTSSetData `json:"data"`
}

// MQTTTTSSetAck is the tts.set ack; status is "starting" | "success" | "failure".
type MQTTTTSSetAck struct {
	MQTTInfoResponse
	Kind   string          `json:"kind"`
	Status string          `json:"status"`
	Error  string          `json:"error,omitempty"`
	Data   *MQTTTTSSetData `json:"data,omitempty"`
}

// RealtimeSetData is the realtime voice-agent config, shared by MQTT realtime.set and HTTP UpdateConfig.
// All fields are optional (omitted = unchanged); model/voice/reasoning apply to the active provider.
type RealtimeSetData struct {
	Enabled   *bool  `json:"enabled,omitempty"`  // nil = leave unchanged
	Provider  string `json:"provider,omitempty"` // gemini | openai | gptlive | pipecat_v1 | none
	Model     string `json:"model,omitempty"`
	Voice     string `json:"voice,omitempty"`
	Reasoning string `json:"reasoning,omitempty"` // gemini thinking_level OR openai reasoning_effort (gptlive / pipecat_v1: none)
	APIKey    string `json:"api_key,omitempty"`   // optional override; empty → llm_api_key
	BaseURL   string `json:"base_url,omitempty"`  // optional override; empty → llm_base_url-derived
	// WebSearch toggles the in-session web_search tool (pipecat_v1 only); nil = unchanged.
	WebSearch *bool `json:"web_search,omitempty"`
}

// MQTTRealtimeSetCommand wraps the full realtime.set downlink envelope for unmarshalling.
type MQTTRealtimeSetCommand struct {
	Data RealtimeSetData `json:"data"`
}

// MQTTRealtimeSetAck is the realtime.set ack; status is "starting" | "success" | "failure".
type MQTTRealtimeSetAck struct {
	MQTTInfoResponse
	Kind   string           `json:"kind"`
	Status string           `json:"status"`
	Error  string           `json:"error,omitempty"`
	Data   *RealtimeSetData `json:"data,omitempty"`
}

// WakeWordGateData controls the top-level wakeword flag; Enabled is a pointer so omission is rejected.
type WakeWordGateData struct {
	Enabled *bool `json:"enabled" validate:"required"`
}

// MQTTWakeWordGateAck is the wakeword.gate ack; status is "starting" | "success" | "failure".
type MQTTWakeWordGateAck struct {
	MQTTInfoResponse
	Kind   string            `json:"kind"`
	Status string            `json:"status"`
	Error  string            `json:"error,omitempty"`
	Data   *WakeWordGateData `json:"data,omitempty"`
}

// AgentRuntimeSetData is the target backend for a runtime switch; unknown values are rejected, never defaulted.
type AgentRuntimeSetData struct {
	Runtime string `json:"runtime"` // one of AgentRuntimes
	// URL and Token configure the external gateway; read only when Runtime == "remote".
	URL   string `json:"url,omitempty"`
	Token string `json:"token,omitempty"`
}

// AgentRuntimes is the valid set of switchable backends.
var AgentRuntimes = []string{AgentRuntimeOpenClaw, AgentRuntimeHermes, AgentRuntimePicoclaw, AgentRuntimeCodex, AgentRuntimeClaudeCode, AgentRuntimeOpenCode, AgentRuntimeRemote}

// IsValidAgentRuntime reports whether r is a switchable backend (case-insensitive, trimmed).
func IsValidAgentRuntime(r string) bool {
	r = strings.ToLower(strings.TrimSpace(r))
	for _, v := range AgentRuntimes {
		if v == r {
			return true
		}
	}
	return false
}

// AgentRuntimeStatus is returned by GET /api/device/agent-runtime.
type AgentRuntimeStatus struct {
	Current string   `json:"current"`
	Options []string `json:"options"`

	// Ready reports whether the backend is answering, not merely selected.
	Ready bool `json:"ready"`

	// RemoteURL and RemoteToken echo the remote config; the token is unredacted (admin-only route).
	RemoteURL   string `json:"remote_url,omitempty"`
	RemoteToken string `json:"remote_token,omitempty"`
}

// TimezoneStatus is returned by GET /api/device/timezone: active zone plus selectable zones.
type TimezoneStatus struct {
	Current string   `json:"current"`
	Zones   []string `json:"zones"`
}

// TimezoneSetData is the IANA zone to apply (e.g. "Asia/Ho_Chi_Minh"), shared by HTTP and MQTT.
type TimezoneSetData struct {
	Timezone string `json:"timezone" validate:"required"`
}

// MQTTTimezoneSetAck is the timezone.set ack; status is "starting" | "success" | "failure".
type MQTTTimezoneSetAck struct {
	MQTTInfoResponse
	Kind   string           `json:"kind"`
	Status string           `json:"status"`
	Error  string           `json:"error,omitempty"`
	Data   *TimezoneSetData `json:"data,omitempty"`
}

// AgentRuntimeSetAck is the <runtime>.setup ack (Kind echoed); success is followed by an os-server restart.
type AgentRuntimeSetAck struct {
	MQTTInfoResponse
	Kind   string               `json:"kind"`
	Status string               `json:"status"`
	Error  string               `json:"error,omitempty"`
	Data   *AgentRuntimeSetData `json:"data,omitempty"`
}

// MQTTFaceEnrollData is the Data payload for kind:"face.enroll"; fields mirror HAL's FaceEnrollRequest.
type MQTTFaceEnrollData struct {
	ImageBase64      string `json:"image_base64"`
	Label            string `json:"label"`
	TelegramUsername string `json:"telegram_username,omitempty"`
	TelegramID       string `json:"telegram_id,omitempty"`
}

// MQTTFaceRemoveData is the Data payload for kind:"face.remove".
type MQTTFaceRemoveData struct {
	Label string `json:"label"`
}

// MQTTVoiceData is the Data payload for kind:"voice.enroll" and kind:"voice.remove".
type MQTTVoiceData struct {
	Label string `json:"label"`
}

// MQTTVoiceEnrollStarting is the `starting` ack data for kind:"voice.enroll" so the app can run a countdown.
type MQTTVoiceEnrollStarting struct {
	Label       string `json:"label"`
	DurationSec int    `json:"duration_sec"`
}

// MQTTVoiceFileData is the Data payload for kind:"voice.file.get" and kind:"voice.file.remove".
type MQTTVoiceFileData struct {
	Label string `json:"label"`
	File  string `json:"file"`
}

// MQTTLEDRestingData is the Data payload for kind:"led.resting.set" and kind:"led.resting.preview".
type MQTTLEDRestingData struct {
	Mode  string `json:"mode"`
	Color []int  `json:"color"`
}

// MQTTVolumeData is the Data payload for kind:"volume.set".
type MQTTVolumeData struct {
	// Volume is 0-100% of the allowed range, like the web slider; nil is rejected.
	Volume *int `json:"volume"`
}

// MQTTVolumeState is the success data for kind:"volume.get" and kind:"volume.set".
type MQTTVolumeState struct {
	Volume    int `json:"volume"`     // 0-100% of the allowed range (what the slider shows)
	Raw       int `json:"raw"`        // mixer percentage HAL applied
	MaxVolume int `json:"max_volume"` // SAFETY.md ceiling; 100 when none is declared
}

// MQTTMicData is the Data payload for kind:"mic.set"; Muted is a pointer so omission is rejected.
type MQTTMicData struct {
	Muted *bool `json:"muted"`
}

// MQTTMicState is the success data for kind:"mic.get" and kind:"mic.set".
type MQTTMicState struct {
	Muted bool `json:"muted"`
	// HWSwitchMuted is the physical mic switch; true blocks unmuting. Null on devices without one.
	HWSwitchMuted *bool `json:"hw_switch_muted"`
	// Available is false when the voice pipeline is down (no mic, or HAL still starting).
	Available bool `json:"available"`
}

// MQTTRealtimeGetData is the success data for kind:"realtime.get".
type MQTTRealtimeGetData struct {
	Config  RealtimePublic         `json:"config"`
	Options config.RealtimeOptions `json:"options"`
}

// MQTTVoiceFileContent is the success data for kind:"voice.file.get".
type MQTTVoiceFileContent struct {
	Label         string `json:"label"`
	File          string `json:"file"`
	ContentType   string `json:"content_type"`
	Size          int    `json:"size"`
	ContentBase64 string `json:"content_base64"`
}

// MQTTTTSPreviewData is the data payload for kind:"tts.preview".
// Text is required; Speed is 0.25-4.0; empty overrides fall back to the device TTS config.
type MQTTTTSPreviewData struct {
	Speed    *float64 `json:"speed,omitempty"`
	Text     string   `json:"text"`
	Provider string   `json:"provider,omitempty"`
	Voice    string   `json:"voice,omitempty"`
	Language string   `json:"language,omitempty"`
}

// MQTTTTSPreviewCommand wraps the full tts.preview downlink envelope for unmarshalling.
type MQTTTTSPreviewCommand struct {
	Data MQTTTTSPreviewData `json:"data"`
}

// MQTTDeviceRenameData is the data payload for kind:"device.rename" (IDENTITY.md **Name:**).
type MQTTDeviceRenameData struct {
	Name string `json:"name"`
}

// RealtimePublic is the resolved realtime config for read-back; the key is exposed only as HasAPIKey.
type RealtimePublic struct {
	Enabled   bool   `json:"enabled"`
	Provider  string `json:"provider"` // "" when realtime is off
	Model     string `json:"model"`
	Voice     string `json:"voice"`
	Reasoning string `json:"reasoning"`
	BaseURL   string `json:"base_url"` // resolved (may be llm-derived)
	HasAPIKey bool   `json:"has_api_key"`
	// WebSearch is set only for pipecat_v1.
	WebSearch *bool `json:"web_search,omitempty"`
}

// ConfigPublicResponse is returned by GET /api/device/config; secrets appear only as Has* presence flags.
type ConfigPublicResponse struct {
	Environment EnvironmentConfig `json:"environment"`

	Channel                  string   `json:"channel"`
	TelegramUserID           string   `json:"telegram_user_id"`
	SlackUserID              string   `json:"slack_user_id"`
	DiscordGuildID           string   `json:"discord_guild_id"`
	DiscordUserID            string   `json:"discord_user_id"`
	WhatsappUserID           string   `json:"whatsapp_user_id"`
	BluebubblesServerURL     string   `json:"bluebubbles_server_url"`
	BluebubblesUserAddress   string   `json:"bluebubbles_user_address"`
	BluebubblesCallerContext string   `json:"bluebubbles_caller_context"`
	LLMModel                 string   `json:"llm_model"`
	LLMBaseURL               string   `json:"llm_base_url"`
	LLMDisableThinking       bool     `json:"llm_disable_thinking"`
	STTBaseURL               string   `json:"stt_base_url"`
	TTSBaseURL               string   `json:"tts_base_url"`
	STTLanguage              string   `json:"stt_language"`
	STTModel                 string   `json:"stt_model"`
	TTSProvider              string   `json:"tts_provider"`
	TTSVoice                 string   `json:"tts_voice"`
	TTSSpeed                 float64  `json:"tts_speed"`
	WakeWord                 bool     `json:"wakeword"`
	AgentName                string   `json:"agent_name"`
	WakePhrases              []string `json:"wake_phrases"`
	DeviceID                 string   `json:"device_id"`
	Mac                      string   `json:"mac"`
	NetworkSSID              string   `json:"network_ssid"`
	MQTTEndpoint             string   `json:"mqtt_endpoint"`
	MQTTUsername             string   `json:"mqtt_username"`
	MQTTPort                 int      `json:"mqtt_port"`
	FAChannel                string   `json:"fa_channel"`
	FDChannel                string   `json:"fd_channel"`

	Realtime RealtimePublic `json:"realtime"`

	// Presence booleans replace raw secret values.
	HasTelegramBotToken    bool `json:"has_telegram_bot_token"`
	HasSlackBotToken       bool `json:"has_slack_bot_token"`
	HasSlackAppToken       bool `json:"has_slack_app_token"`
	HasDiscordBotToken     bool `json:"has_discord_bot_token"`
	HasBluebubblesPassword bool `json:"has_bluebubbles_password"`
	HasLLMAPIKey           bool `json:"has_llm_api_key"`
	HasDeepgramAPIKey      bool `json:"has_deepgram_api_key"`
	HasSTTAPIKey           bool `json:"has_stt_api_key"`
	HasTTSAPIKey           bool `json:"has_tts_api_key"`
	HasNetworkPassword     bool `json:"has_network_password"`
	HasMQTTPassword        bool `json:"has_mqtt_password"`
	HasAdminPassword       bool `json:"has_admin_password"`

	// HasAutonomousDefaults is true once the shipped credential set has been preserved.
	HasAutonomousDefaults bool `json:"has_autonomous_defaults"`

	// Non-secret half of the stored default set; the key is never returned.
	AutonomousDefaultBaseURL string `json:"autonomous_default_base_url,omitempty"`
	AutonomousDefaultModel   string `json:"autonomous_default_model,omitempty"`
}

// UpdateConfigRequest is used by PUT /api/device/config; only non-empty values are applied.
type UpdateConfigRequest struct {
	Environment *EnvironmentConfig `json:"environment,omitempty"`

	SSID     string `json:"ssid"`
	Password string `json:"password"`
	Channel  string `json:"channel"`

	TelegramBotToken string `json:"telegram_bot_token"`
	TelegramUserID   string `json:"telegram_user_id"`

	SlackBotToken string `json:"slack_bot_token"`
	SlackAppToken string `json:"slack_app_token"`
	SlackUserID   string `json:"slack_user_id"`

	DiscordBotToken string `json:"discord_bot_token"`
	DiscordGuildID  string `json:"discord_guild_id"`
	DiscordUserID   string `json:"discord_user_id"`

	WhatsappUserID string `json:"whatsapp_user_id"`

	// iMessage plain fields distinguish omitted (preserve) from empty (clear).
	// The password retains the existing empty-means-preserve secret contract.
	BluebubblesServerURL     *string `json:"bluebubbles_server_url"`
	BluebubblesPassword      string  `json:"bluebubbles_password"`
	BluebubblesUserAddress   *string `json:"bluebubbles_user_address"`
	BluebubblesCallerContext *string `json:"bluebubbles_caller_context"`

	LLMBaseURL         string `json:"llm_base_url"`
	LLMAPIKey          string `json:"llm_api_key"`
	LLMModel           string `json:"llm_model"`
	LLMDisableThinking *bool  `json:"llm_disable_thinking,omitempty"`

	DeepgramAPIKey string `json:"deepgram_api_key"`
	STTAPIKey      string `json:"stt_api_key"`
	TTSAPIKey      string `json:"tts_api_key"`
	// ClearTTSAPIKey deletes the stored TTS key (an empty TTSAPIKey means "not sent").
	ClearTTSAPIKey bool   `json:"clear_tts_api_key"`
	STTBaseURL     string `json:"stt_base_url"`
	TTSBaseURL     string `json:"tts_base_url"`
	STTLanguage    string `json:"stt_language"`
	DeviceID       string `json:"device_id"`

	MQTTEndpoint string `json:"mqtt_endpoint"`
	MQTTUsername string `json:"mqtt_username"`
	MQTTPassword string `json:"mqtt_password"`
	MQTTPort     int    `json:"mqtt_port"`
	FAChannel    string `json:"fa_channel"`
	FDChannel    string `json:"fd_channel"`

	TTSProvider string   `json:"tts_provider"`
	TTSVoice    string   `json:"tts_voice"`
	TTSSpeed    *float64 `json:"tts_speed,omitempty" binding:"omitempty,gte=0.25,lte=4"`
	WakeWord    *bool    `json:"wakeword,omitempty"`

	// Realtime is the same payload as MQTT realtime.set; omit to leave it unchanged.
	Realtime *RealtimeSetData `json:"realtime,omitempty"`

	// AdminPassword rotates the bcrypt hash; existing sessions survive until SessionSecret rotates.
	AdminPassword string `json:"admin_password"`
}

// TTS provider constants.
const (
	TTSProviderOpenAI     = "openai"
	TTSProviderElevenLabs = "elevenlabs"
	// TTSProviderPiper synthesizes on-device (no base URL or API key).
	TTSProviderPiper = "piper"
	// TTSProviderGemini uses Gemini TTS via the autonomous proxy relay.
	TTSProviderGemini = "gemini"
)

// DefaultGeminiVoice is seeded when gemini has no voice; must stay in HAL's GeminiTTSBackend.VOICES.
const DefaultGeminiVoice = "Kore"

// TTSProviders is the list of supported TTS providers.
var TTSProviders = []string{TTSProviderOpenAI, TTSProviderElevenLabs, TTSProviderPiper, TTSProviderGemini}

// IsValidTTSProvider reports whether p is a supported TTS provider.
func IsValidTTSProvider(p string) bool {
	for _, v := range TTSProviders {
		if v == p {
			return true
		}
	}
	return false
}

// DefaultElevenLabsVoiceForLang returns the default ElevenLabs voice for lang (prefix match).
// Names must stay in sync with HAL's elevenlabs.py VOICE_IDS_BY_LANG.
func DefaultElevenLabsVoiceForLang(lang string) string {
	switch {
	case strings.HasPrefix(lang, "vi"):
		return "Ngan"
	case strings.HasPrefix(lang, "zh"):
		return "Amy"
	default:
		return "Rachel"
	}
}

// TTSVoicesByProvider maps provider name to its available voices.
var TTSVoicesByProvider = map[string][]string{
	TTSProviderOpenAI: {"alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"},
	// Empty on purpose: Piper voices are downloaded on demand and listed live by HAL.
	TTSProviderPiper: {},
	// Mirrors HAL's GeminiTTSBackend.VOICES.
	TTSProviderGemini:     {"Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat"},
	TTSProviderElevenLabs: {"Rachel", "Sarah", "Grace", "Freya", "Matilda", "Emily", "Alice", "Lily", "Charlotte", "Nicole", "Glinda", "Serena", "Jessie", "Brian", "Adam", "Daniel", "George", "James", "Liam", "Callum", "Harry", "Charlie", "Chris", "Sam"},
}

// TTSVoices is the default (OpenAI) voice list for backward compatibility.
var TTSVoices = TTSVoicesByProvider[TTSProviderOpenAI]

// DefaultTTSVoice is the default voice when none is configured.
const DefaultTTSVoice = "alloy"
