package domain

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

// Channel type identifiers. WhatsApp is intentionally NOT a valid Setup
// channel — it can only be added post-setup via the MQTT add_channel command
// because pairing is interactive (QR streaming) and the captive-portal setup
// path can't carry a live event stream.
const (
	ChannelTelegram = "telegram"
	ChannelSlack    = "slack"
	ChannelDiscord  = "discord"
	ChannelWhatsapp = "whatsapp"
)

type SetupRequest struct {
	// Network credentials. Neither is required:
	//   - Empty SSID means "the device already has a working uplink, don't join
	//     any WiFi" — the ethernet case. device.Setup verifies there really is
	//     internet before accepting it, and tears down the provisioning AP.
	//   - Empty Password with a non-empty SSID is an open network.
	SSID     string `json:"ssid"`
	Password string `json:"password"`

	// channel type: "telegram" (default), "slack" or "discord".
	// WhatsApp is intentionally not accepted here — it must be added
	// post-setup via the MQTT add_channel command (streaming QR pairing).
	Channel string `json:"channel"`

	// telegram channel (required when channel is telegram or empty)
	TelegramBotToken string `json:"telegram_bot_token"`
	TelegramUserID   string `json:"telegram_user_id"`

	// slack channel (required when channel is slack)
	SlackBotToken string `json:"slack_bot_token"`
	SlackAppToken string `json:"slack_app_token"`
	SlackUserID   string `json:"slack_user_id"`

	// discord channel (required when channel is discord)
	DiscordBotToken string `json:"discord_bot_token"`
	DiscordGuildID  string `json:"discord_guild_id"`
	DiscordUserID   string `json:"discord_user_id"`

	// setup custom provider for openclaw
	LLMBaseURL string `json:"llm_base_url" validate:"required"`
	LLMAPIKey  string `json:"llm_api_key" validate:"required"`
	LLMModel   string `json:"llm_model"`

	// voice pipeline (optional): Deepgram API key for STT
	DeepgramAPIKey string `json:"deepgram_api_key"`
	// STTAPIKey / TTSAPIKey override LLMAPIKey when those accounts are
	// separate. Empty = device falls back to LLMAPIKey. STTBaseURL /
	// TTSBaseURL likewise override LLMBaseURL.
	STTAPIKey   string `json:"stt_api_key"`
	TTSAPIKey   string `json:"tts_api_key"`
	STTBaseURL  string `json:"stt_base_url"`
	TTSBaseURL  string `json:"tts_base_url"`
	STTLanguage string `json:"stt_language"`
	TTSProvider string `json:"tts_provider"`
	TTSVoice    string `json:"tts_voice"`

	// optional
	DeviceID string `json:"device_id" validate:"required"`

	// AdminPassword is the plaintext password the operator picks at setup time.
	// Server bcrypts it into config.AdminPasswordHash and never persists the
	// plaintext. Used to gate browser admin access via POST /api/login + the
	// os_session cookie. Empty allowed (validated at handler level so
	// pre-login-UI clients keep working during the migration window).
	AdminPassword string `json:"admin_password"`

	// MQTT (optional): empty broker URL means MQTT disabled
	MQTTEndpoint string `json:"mqtt_endpoint"`
	MQTTUsername string `json:"mqtt_username"`
	MQTTPassword string `json:"mqtt_password"`
	MQTTPort     int    `json:"mqtt_port"`
	FAChannel    string `json:"fa_channel"`
	FDChannel    string `json:"fd_channel"`

	// LLMDisableThinking disables extended thinking/reasoning for all models (default false).
	LLMDisableThinking *bool `json:"llm_disable_thinking,omitempty"`
}

// WifiProvisionRequest is the payload for POST /api/device/wifi-provision — the
// AP-portal fast path that runs when the operator is standing next to the
// device on its own hotspot. Purpose:
//
//   - Re-point the device at a different home Wi-Fi (moved offices, changed
//     router, mistyped the password on original setup).
//   - Or bring a device online end-to-end when autonomous.ai (the URL-push
//     origin) is unreachable — the operator types their own LLM credentials
//     into the same screen and the device sets itself up with those.
//
// All fields EXCEPT `SSID` are optional. Empty fields are read as "leave the
// existing on-disk value alone" (mergeMissingFromConfig applies for the sub-
// set that overlaps SetupRequest). Deliberately NOT a SetupRequest with
// optional fields: SetupRequest.LLMAPIKey / LLMBaseURL / DeviceID carry
// `validate:"required"` because a full first-time setup legitimately needs
// them, and loosening those tags would silently let a fresh provision go
// through with no LLM at all. Separate endpoint + separate type keeps both
// semantics honest.
//
// A completely fresh device (no LLM on disk yet) still needs the operator to
// type LLM creds here — service.ReprovisionWifi only runs agent setup when
// creds are present, and the device just sits in "Wi-Fi connected but not
// finished" until they are.
type WifiProvisionRequest struct {
	SSID     string `json:"ssid" validate:"required"`
	Password string `json:"password"` // empty = open network

	// Optional overrides. Empty = leave the on-disk value alone.
	LLMAPIKey  string `json:"llm_api_key"`
	LLMBaseURL string `json:"llm_base_url"`
	LLMModel   string `json:"llm_model"`

	// Voice pipeline (all optional; empty = keep existing).
	DeepgramAPIKey string `json:"deepgram_api_key"`
	STTAPIKey      string `json:"stt_api_key"`
	STTBaseURL     string `json:"stt_base_url"`
	STTLanguage    string `json:"stt_language"`
	TTSAPIKey      string `json:"tts_api_key"`
	TTSBaseURL     string `json:"tts_base_url"`
	TTSProvider    string `json:"tts_provider"`
	TTSVoice       string `json:"tts_voice"`

	// AdminPassword: plaintext operator-picked password. bcrypted into
	// config.AdminPasswordHash. Empty on a re-provision = keep current
	// hash; empty on a fresh device = handler defaults to the hardware
	// suffix (same policy as SetupRequest / handler.Setup).
	AdminPassword string `json:"admin_password"`

	// Messaging channel (optional). Empty Channel = keep on-disk value.
	// Per-channel token fields are only written when Channel matches; this
	// prevents an operator switching from telegram→slack from leaving the
	// old telegram tokens sitting alongside the new slack credentials in
	// config.json.
	Channel          string `json:"channel"`
	TelegramBotToken string `json:"telegram_bot_token"`
	TelegramUserID   string `json:"telegram_user_id"`
	SlackBotToken    string `json:"slack_bot_token"`
	SlackAppToken    string `json:"slack_app_token"`
	SlackUserID      string `json:"slack_user_id"`
	DiscordBotToken  string `json:"discord_bot_token"`
	DiscordGuildID   string `json:"discord_guild_id"`
	DiscordUserID    string `json:"discord_user_id"`
}

// EffectiveChannel returns the resolved channel type, defaulting to "telegram".
// ChannelWhatsapp is intentionally NOT handled here — setup falls back to
// telegram if whatsapp is somehow requested via this path.
func (r *SetupRequest) EffectiveChannel() string {
	if r.Channel == ChannelSlack {
		return ChannelSlack
	}
	if r.Channel == ChannelDiscord {
		return ChannelDiscord
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
	// channel type: "telegram", "slack", "discord" or "whatsapp"
	Channel string `json:"channel" validate:"required"`

	// telegram
	TelegramBotToken string `json:"telegram_bot_token"`
	TelegramUserID   string `json:"telegram_user_id"`

	// slack
	SlackBotToken string `json:"slack_bot_token"`
	SlackAppToken string `json:"slack_app_token"`
	SlackUserID   string `json:"slack_user_id"`
	// SlackMode selects the Slack transport: "socket" (default, OpenClaw opens
	// outbound WSS to Slack — needs SlackAppToken) or "http" (OpenClaw listens
	// for POSTs forwarded from a public proxy — needs SlackSigningSecret).
	// HTTP mode is the message-loss-tolerant path: a public proxy
	// (bff-campaign-service) receives Slack events, fans out via MQTT to the
	// device's slack_event handler, which POSTs to localhost OpenClaw.
	SlackMode          string `json:"slack_mode,omitempty"`
	SlackSigningSecret string `json:"slack_signing_secret,omitempty"`
	SlackWebhookPath   string `json:"slack_webhook_path,omitempty"` // optional, defaults to /slack/events when SlackMode=http

	// discord
	DiscordBotToken string `json:"discord_bot_token"`
	DiscordGuildID  string `json:"discord_guild_id"`
	DiscordUserID   string `json:"discord_user_id"`

	// whatsapp — bot login is handled interactively by the Baileys CLI; only
	// the operator's E.164 phone number (the permitted DM caller) ships here.
	WhatsappUserID string `json:"whatsapp_user_id"`
}

// RefreshChannelRequest carries the credentials needed to re-apply a channel's
// canonical config block via AgentGateway.RefreshChannelConfig. The caller
// (device.Service.RefreshChannelConfig) sources the creds from config.json on
// the device — refresh does NOT carry tokens over MQTT.
type RefreshChannelRequest struct {
	Channel string

	// telegram
	TelegramBotToken string
	TelegramUserID   string

	// slack
	SlackBotToken string
	SlackAppToken string
	SlackUserID   string
	// SlackMode selects the Slack transport — see AddChannelRequest.SlackMode for
	// semantics. Empty / "socket" preserves existing behaviour; "http" switches to
	// proxy mode and consults SlackSigningSecret / SlackWebhookPath.
	SlackMode          string
	SlackSigningSecret string
	SlackWebhookPath   string

	// discord
	DiscordBotToken string
	DiscordGuildID  string
	DiscordUserID   string
}

// EffectiveSlackMode resolves SlackMode, defaulting to "socket" so unset
// payloads keep current behaviour (existing installs unaffected).
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
			// SlackAppToken not used in HTTP mode (Socket Mode only).
		default: // "socket"
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
// Matches spec: docs/mqtt_specs_autonomous.md
const (
	CommandInfo         = "info"
	CommandAddChannel   = "add_channel"
	CommandOTA          = "ota"
	CommandData         = "data"
	CommandWhatsappPair = "whatsapp_pair"

	// CommandClaudeCodeLogin starts the claude.ai OAuth login flow on the
	// claudecode runtime (ClaudeLoginPairer — see domain/claudelogin.go). The
	// device streams pairing events on fd_channel: pairing_starting →
	// pairing_url (login_url field carries the URL the user must open) →
	// success | timeout | failure. The authorization code the user copies from
	// the browser comes back via CommandClaudeCodeLoginCode:
	//
	//	{"cmd":"claudecode_login"}
	//	{"cmd":"claudecode_login_code","code":"<pasted code>"}
	//
	// On success the OAuth token is persisted to config.json
	// (claude_code_oauth_token) and presync switches the runtime to
	// subscription auth (no ANTHROPIC_* API-key env).
	CommandClaudeCodeLogin     = "claudecode_login"
	CommandClaudeCodeLoginCode = "claudecode_login_code"

	// CommandSlackEvent is sent by the public Slack-events proxy (bff-campaign-service)
	// when Slack delivers an Events API POST for a workspace this device owns. Payload
	// is a verbatim forward of Slack's HTTP request body + signature headers; this
	// device POSTs them to the local OpenClaw gateway's /slack/events endpoint, which
	// re-verifies the Slack signature (we don't strip / re-sign in the proxy because
	// OpenClaw owns the signing-secret check by design).
	//
	// Wire format: {"cmd":"slack_event","event_id":"Ev123","body":"<raw JSON>",
	//               "headers":{"X-Slack-Signature":"v0=...","X-Slack-Request-Timestamp":"...",
	//                          "Content-Type":"application/json"}}
	//
	// Devices configured for socket-mode Slack will silently 404 on the local POST
	// (gateway has no /slack/events route in that mode) — proxy SHOULD route only to
	// devices the backend has flipped to slack_mode="http".
	CommandSlackEvent = "slack_event"

	// CommandSlackCommand is sent by the same Slack proxy (bff-campaign-service)
	// when Slack delivers a slash-command invocation (/openclaw, /new, ...) for a
	// workspace this device owns. It is forwarded and verified exactly like
	// CommandSlackEvent — POSTed verbatim to the local OpenClaw gateway's single
	// HTTP webhook (default http://127.0.0.1:18789/slack/events), which routes it
	// to the slash-command handler by body shape and replies to the user via the
	// command's response_url. The only wire differences from slack_event are the
	// urlencoded Content-Type the proxy sets in headers and that the event_id slot
	// carries Slack's trigger_id (slash commands have no event_id).
	//
	// Wire format: {"cmd":"slack_command","event_id":"<trigger_id>","body":"<raw urlencoded form>",
	//               "headers":{"X-Slack-Signature":"v0=...","X-Slack-Request-Timestamp":"...",
	//                          "Content-Type":"application/x-www-form-urlencoded"}}
	//
	// Like slack_event, only relevant for devices in slack_mode="http".
	CommandSlackCommand = "slack_command"
)

// Data kinds carried inside CommandData envelope.
const (
	KindBuddyPairStart      = "buddy.pair.start"  // issue the shared 6-digit Buddy pairing code (60s)
	KindBuddyStatus         = "buddy.status"      // query or report the current public Buddy state
	KindBuddyPairRevoke     = "buddy.pair.revoke" // revoke the current Buddy pairing
	KindHarnessPairStart    = "harness.pair.start"
	KindHarnessStatus       = "harness.status"
	KindHarnessVoiceModeGet = "harness.voice-mode.get"
	KindHarnessVoiceModeSet = "harness.voice-mode.set"
	KindHarnessPairCancel   = "harness.pair.cancel"
	KindHarnessPairRevoke   = "harness.pair.revoke"

	KindTTSSet       = "tts.set"       // persist TTS voice/provider/language config
	KindTTSPreview   = "tts.preview"   // one-shot TTS preview, no config write
	KindDeviceRename = "device.rename" // rewrite IDENTITY.md Name (WatchIdentity picks up wake-words)
	KindOAuthSet     = "oauth.set"     // store/replace OAuth token for a provider
	KindOAuthRemove  = "oauth.remove"  // delete OAuth token for a provider
	KindRealtimeSet  = "realtime.set"  // persist realtime voice-agent config (provider/voice/reasoning…)
	KindWakeWordGate = "wakeword.gate" // persist the top-level wake-word gate
	KindTimezoneSet  = "timezone.set"  // apply device IANA timezone (/etc/localtime + /etc/timezone)
	// KindDeviceSoftReset wipes the device's config.json and restarts os-server so
	// the device drops back into AP setup mode WITHOUT rebooting or rolling back
	// the firmware. Faster and safer than the hard factory-reset button on the
	// device: keeps the current firmware and skips the reboot delay. Payload is
	// empty; the handler acks then triggers the wipe/restart asynchronously so
	// the ack has time to fly before os-server tears down. Used by the
	// "Soft reset" action on autonomous.ai/internpro/me (and Lamp equivalent).
	KindDeviceSoftReset = "device.soft_reset"

	// KindHermesSetup / KindPicoclawSetup / KindClaudecodeSetup / KindOpenclawSetup
	// / KindCodexSetup / KindOpenCodeSetup
	// switch the active agentic backend. The kind itself names the target runtime —
	// the worker (stand-to-earn-worker's steoauthkind.HermesSetup / PicoclawSetup /
	// OpenclawSetup) publishes the backend-specific kind instead of a generic
	// envelope carrying a runtime field. All funnel into
	// device.Service.UpdateAgentRuntime, which persists config.agent_runtime then
	// runs switch-runtime.sh (toggle systemd units + restart os-server so
	// agent/factory.go re-resolves the gateway). The device acks each on
	// fd_channel with the same kind. Replaces the former generic agent_runtime.set.
	// openclaw.setup is the revert path (hermes/picoclaw/claudecode → openclaw,
	// the baked baseline).
	KindHermesSetup     = "hermes.setup"
	KindPicoclawSetup   = "picoclaw.setup"
	KindClaudecodeSetup = "claudecode.setup"
	KindOpenclawSetup   = "openclaw.setup"
	KindCodexSetup      = "codex.setup"
	KindOpenCodeSetup   = "opencode.setup"

	// AgentRuntimeOpenClaw / AgentRuntimeHermes / AgentRuntimePicoclaw /
	// AgentRuntimeCodex / AgentRuntimeClaudeCode / AgentRuntimeOpenCode are the
	// swappable agentic backends. Source of truth mirrored by
	// system/agent/factory.go's resolver and /usr/local/bin/switch-runtime.
	AgentRuntimeOpenClaw   = "openclaw"
	AgentRuntimeHermes     = "hermes"
	AgentRuntimePicoclaw   = "picoclaw"
	AgentRuntimeCodex      = "codex"
	AgentRuntimeClaudeCode = "claudecode"
	AgentRuntimeOpenCode   = "opencode"
	// AgentRuntimeRemote — device is only a voice/chat frontend, the brain
	// runs on another machine (typically the user's Mac). Phase A: the value
	// is selectable in the UI and the URL/token are persisted, but the
	// runtime switch itself is not wired end-to-end yet — SetAgentRuntime
	// saves the config and returns without touching switch-runtime, so
	// factory.go still resolves to whatever runtime is actually installed.
	AgentRuntimeRemote = "remote"

	KindSystemInfo     = "system.info"     // aggregate: versions + network + host
	KindSystemVersion  = "system.version"  // lamp + bootstrap + hal + openclaw versions
	KindSystemNetwork  = "system.network"  // IP, MAC, SSID, gateway of the default-route interface
	KindSystemReboot   = "system.reboot"   // cue-aware OS reboot via HAL
	KindSystemShutdown = "system.shutdown" // cue- and servo-aware OS shutdown via HAL

	// KindSystemOTAVersions reports per-component current vs published versions
	// plus what bootstrap is installing right now — the cloud twin of the web
	// Versions card (GET /api/system/ota-versions + /ota-updating).
	KindSystemOTAVersions = "system.ota_versions"
	// KindSystemSoftwareUpdate force-updates one component. Data:
	// MQTTSoftwareUpdateData. The cloud twin of
	// POST /api/system/software-update/:target; replies "success" (started)
	// immediately, then publishes an unsolicited completion report.
	KindSystemSoftwareUpdate = "system.software_update"

	// KindSkillsInstall installs a role's skill bundle. Data: {"role":"<role>"}.
	KindSkillsInstall = "skills.install"

	// KindSkillsSave writes ONE authored skill into the active runtime's skills
	// dir. Data: MQTTSkillsSaveData. The MQTT twin of POST /api/agent/skills —
	// same AgentGateway.SaveSkill call, so both paths land in the same place and
	// honour the same no-overwrite rule.
	KindSkillsSave = "skills.save"

	// KindSkillsUpload installs a .md, .zip, or .skill supplied inline by the
	// backend. Data: MQTTSkillsUploadData. This mirrors
	// POST /api/agent/skills/upload.
	KindSkillsUpload = "skills.upload"

	// KindSkillsInstallStore installs ONE skill from the Autonomous skill catalog by
	// id. Data: MQTTSkillsInstallStoreData. The MQTT twin of
	// POST /api/agent/skills/install — the device downloads the `.skill` archive
	// and the ACTIVE runtime extracts it (AgentGateway.InstallSkillArchive), so it
	// works on every backend.
	//
	// Named "install" to match the device API it mirrors, with the _store suffix
	// only because the bare skills.install kind is already taken by the older,
	// different feature: a whole ROLE bundle, openclaw-only.
	KindSkillsInstallStore = "skills.install_store"

	// KindSkillsFiles reads ONE installed skill's files. Data:
	// MQTTSkillsFilesData. The MQTT twin of GET /api/agent/skills/files, which is
	// a LAN-only admin endpoint — this is how the backend (and through it a mobile
	// app) inspects a skill the `skills` uplink advertised.
	//
	// Two modes, because MQTT is not a bulk transport: without `path` it returns
	// the file LIST with no contents; with `path` it returns that one file's text.
	KindSkillsFiles = "skills.files"

	// KindSkillsUninstall removes ONE installed skill. Data:
	// MQTTSkillsUninstallData. The MQTT twin of DELETE /api/agent/skills.
	KindSkillsUninstall = "skills.uninstall"

	// KindChannelRefreshConfig re-applies the canonical channels.<channel> block on
	// an already-onboarded device. Targets older customers whose openclaw.json
	// predates schema additions (e.g. the socketMode block, object-form streaming,
	// dmPolicy) — backend pushes this so the device rewrites the block using the
	// current applySlackChannelConfig writer without a full re-onboarding flow.
	//
	// Separate from add_channel by design: refresh is config-only (no plugin
	// install, no CLI bootstrap, no pairing). Today only channel:"slack" is
	// implemented; other channels return a distinct error code so the backend can
	// branch. Credentials are read from config.json on the device — they are NOT
	// carried in the payload.
	//
	// Flow:
	//   server → device : kind=channel.refresh_config data={channel}
	//   device → server : status=configuring → status=success data={channel, runtime}
	//                                        | status=failure error=<code>
	KindChannelRefreshConfig = "channel.refresh_config"

	// KindChatSend starts an agent turn from the backend — the MQTT twin of the
	// web monitor's POST /api/sensing/event, forwarded as type "mqtt_chat"
	// (same gates as the monitor's "web_chat", distinct only so the Monitor's
	// turn flow shows which chat the turn came from). Data:
	// MQTTChatSendData. This is what lets a phone app hold the SAME conversation
	// the web chat holds: the device is on a LAN behind NAT, so MQTT is the only
	// standing path in, and fa/fd are already per-device.
	//
	// Flow:
	//   server → device : kind=chat.send data={message, image?, session_id?, speak?}
	//   device → server : status=success data={run_id, session_id}
	//                     then a STREAM of kind=chat.event carrying that run's
	//                     monitor events. chat_response fires REPEATEDLY as the
	//                     reply streams; only the one whose State is
	//                     complete/final/error ends the run.
	//
	// Deliberately NOT the same as the one-way autonomous-chat-hook bridge, which
	// forwards as type "voice": that makes the device SPEAK the reply and returns
	// nothing to the backend, so it can never back a chat UI.
	KindChatSend = "chat.send"

	// KindChatEvent is DEVICE-INITIATED (no request carries it): one monitor
	// event belonging to a chat.send run. Data: MQTTChatEventData.
	//
	// The payload is domain.MonitorEvent verbatim — the same struct the web
	// monitor's SSE stream (GET /api/agent/events) delivers — so a phone client
	// can reuse the web chat's reducer as-is instead of a parallel vocabulary
	// that would drift the first time an event type is added.
	KindChatEvent = "chat.event"

	// KindChatFileGet fetches ONE device-local file a turn named. Data:
	// MQTTChatFileGetData; the reply's Data is MQTTChatFileData.
	//
	// PULL, not push, and deliberately so: it is the MQTT twin of
	// GET /api/agent/file, which is exactly how the web chat works — the client
	// spots a device path in the message it is rendering and asks for that file.
	// Keeping the two the same means a phone bytes-for-bytes reuses the web
	// client's logic, files nobody opens cost nothing on the uplink, and a
	// conversation scrolled back weeks still resolves its images.
	//
	// What may leave the device is decided by system/agentfile — the same
	// allow-list the HTTP endpoint enforces, so `path` being client-supplied is
	// safe the same way it is safe there.
	KindChatFileGet = "chat.file.get"

	// KindScheduleSync replaces the device's entire Scheduled task list. Full-state
	// by design: the backend's list is authoritative, so reconnect, OTA-gate-opening
	// and drift repair are all the same message. Acks with the computed next_run_at
	// per schedule so the backend can render "Next run in 16 hours".
	KindScheduleSync = "schedule.sync"

	// KindScheduleRun runs ONE stored schedule immediately (the "Run now" button).
	// Does not change the schedule's cadence or its next_run_at.
	KindScheduleRun = "schedule.run"

	// KindScheduleMutate is OUTBOUND (fd_channel) — the only schedule kind the
	// device originates rather than answers. Published when the user creates,
	// edits or deletes a task from this unit's own Settings or chat UI. It is a
	// PROPOSAL: the backend applies it under an idempotency key and a
	// compare-and-swap, and the resulting schedule.sync is what the device
	// treats as truth. See system/schedule/intent.go.
	KindScheduleMutate = "schedule.mutate"

	// KindScheduleMutateAck is INBOUND (fa_channel) — the backend's verdict on
	// one proposal, carrying its intent_id. It exists so the device knows when
	// to stop retrying: a queued intent is dropped on ANY terminal verdict,
	// applied or rejected. Without it a proposal the backend refuses would be
	// replayed on every reconnect for the life of the device.
	KindScheduleMutateAck = "schedule.mutate.ack"
)

// Connector (MCP) data-kind prefixes. The connector code is the suffix, e.g.
// "connector.set.notion" / "connector.remove.github". dispatchData prefix-matches
// these before the exact-kind switch.
const (
	DataKindConnectorSetPrefix    = "connector.set."
	DataKindConnectorRemovePrefix = "connector.remove."
)

// Message is the standard envelope for MQTT messages from the server (fa_channel).
// Server sends: {"cmd": "info"}, {"cmd": "add_channel", ...}, {"cmd": "data", "kind": "tts.set", ...}
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
// Example: {"cmd":"add_channel","channel":"discord","config":{"bot_token":"...","guild_id":"..."}}
type MQTTAddChannelCommand struct {
	Channel string                 `json:"channel"`
	Config  map[string]interface{} `json:"config"`
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
		// HTTP-mode proxy fields (optional; omitted payloads keep Socket Mode behaviour).
		req.SlackMode, _ = cfg["mode"].(string)
		req.SlackSigningSecret, _ = cfg["signing_secret"].(string)
		req.SlackWebhookPath, _ = cfg["webhook_path"].(string)
	case ChannelWhatsapp:
		req.WhatsappUserID, _ = cfg["user_id"].(string)
	default:
		req.TelegramBotToken, _ = cfg["bot_token"].(string)
		req.TelegramUserID, _ = cfg["chat_id"].(string)
	}
	return req
}

// MQTTAddChannelResponse extends MQTTInfoResponse with channel-specific fields for fd_channel.
//
// For non-whatsapp channels we publish exactly one message with Status=success|failure.
// For whatsapp the pairing flow is streamed: one message each for
// pairing_starting → pairing_qr (1+) → success | timeout | failure.
// PairingQR* fields are populated only on Status="pairing_qr"; the QR text is
// a multi-line Unicode-block rendering — see PairingQRFormat.
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
// No fields today; reserved for future per-account selection.
type MQTTWhatsappPairCommand struct{}

// MQTTWhatsappPairResponse mirrors MQTTAddChannelResponse but for re-pair flows
// that don't re-bootstrap the channel. Same streaming shape:
// pairing_starting → pairing_qr (1+) → success | timeout | failure.
type MQTTWhatsappPairResponse struct {
	MQTTInfoResponse
	Status           string `json:"status"`
	Error            string `json:"error,omitempty"`
	PairingQRText    string `json:"pairing_qr_text,omitempty"`
	PairingQRFormat  string `json:"pairing_qr_format,omitempty"`
	PairingQRSeq     int    `json:"pairing_qr_seq,omitempty"`
	PairingExpiresAt string `json:"pairing_expires_at,omitempty"`
}

// MQTTClaudeCodeLoginCodeCommand is the fa_channel payload for
// cmd:"claudecode_login_code" — the OAuth authorization code the user copied
// from the browser, fed back into the waiting login flow.
type MQTTClaudeCodeLoginCodeCommand struct {
	Code string `json:"code"`
}

// MQTTClaudeCodeLoginResponse mirrors MQTTWhatsappPairResponse for the
// claude.ai OAuth login flow (CommandClaudeCodeLogin). Streaming shape:
// pairing_starting → pairing_url (login_url set) → success | timeout | failure.
// Also used to ack claudecode_login_code submissions.
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

// DeviceMessage is the base response published to fd_channel.
// All messages MUST include these required fields per spec.
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
	// WakeWordEnabled is the effective top-level wake-word gate from config. It is
	// intentionally not omitted so MQTT consumers can distinguish disabled
	// from an older device that does not report the setting.
	WakeWordEnabled bool `json:"wakeword_enabled"`
	// Timezone is the device's active IANA zone (e.g. "Asia/Ho_Chi_Minh"). The
	// base constructor seeds it from config (the record); the info / system.info
	// handlers override it with the live system value via device.Service.
	Timezone        string `json:"timezone,omitempty"`
	HalVersion      string `json:"hal_version,omitempty"`
	OpenClawVersion string `json:"openclaw_version,omitempty"`
	// HermesVersion sits next to openclaw_version: the installed Hermes CLI version
	// (e.g. "0.17.0"), empty when hermes isn't installed. agent_runtime says which
	// one is actually active.
	HermesVersion string `json:"hermes_version,omitempty"`
	// PicoclawVersion mirrors hermes_version for the PicoClaw backend: the
	// installed picoclaw binary version, empty when not installed.
	PicoclawVersion string `json:"picoclaw_version,omitempty"`
	// CodexVersion mirrors hermes_version for the Codex backend: the installed
	// Codex CLI version (e.g. "0.142.5"), empty when codex isn't installed.
	CodexVersion string `json:"codex_version,omitempty"`
	// ClaudeCodeVersion mirrors codex_version for the Claude Code backend: the
	// installed Claude Code CLI version (e.g. "2.1.83"), empty when not installed.
	ClaudeCodeVersion string `json:"claudecode_version,omitempty"`
	// OpenCodeVersion mirrors codex_version for the OpenCode backend: the
	// installed opencode CLI version, empty when opencode isn't installed.
	OpenCodeVersion string `json:"opencode_version,omitempty"`
	AgentRuntime    string `json:"agent_runtime,omitempty"`
	LocalIP         string `json:"local_ip,omitempty"`
	// UnsupportedChannels lists channels configured in config.json that the active
	// runtime cannot run (populated by ChannelReconcile after a runtime switch — e.g.
	// slack/discord become unsupported after switching to picoclaw). Empty/omitted
	// when every configured channel is supported.
	UnsupportedChannels []string `json:"unsupported_channels,omitempty"`
	// Skills is what the ACTIVE runtime currently has installed — the same set
	// the web UI's Manage-skills panel shows, and the same `skills` array the
	// HTTP backend ping carries (both use SkillSummary, so the two uplinks can't
	// drift). Populated only by handleInfo; omitempty keeps it out of the `data`
	// replies that embed this struct.
	Skills []SkillSummary `json:"skills,omitempty"`
	// SchedulesDigest fingerprints the scheduled-task rows this device holds
	// (schedule.Digest: "v1:" + sha256 over each row's id|rev|requires). The
	// backend compares it with the same digest over its own rows and forces a
	// full schedule.sync when they differ — the only way a device that was
	// reset, lost schedules.json, or was swapped under the same record gets its
	// schedules back, since schedule.sync is otherwise sent only on edits.
	// Populated only by handleInfo, and omitted when the store cannot be read
	// (never "no schedules" by mistake); omitempty keeps it out of the `data`
	// replies that embed this struct, and old firmware never sends it.
	SchedulesDigest string `json:"schedules_digest,omitempty"`
}

// NewDeviceMessage creates a base message with required fields populated from config.
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

// MQTTDataCommand is the fa_channel payload for cmd:"data" — a generic envelope.
// Sub-handlers branch on Kind and unmarshal Data into a kind-specific struct.
//
// Type selects the delivery path:
//   - "" (default)  → Data is inline; dispatch immediately.
//   - "privacy"     → Data is omitted on the broker and lives on the backend;
//     the device acks "received" then fetches it over TLS from
//     /devices/get-message before dispatching (see privacy_fetch.go).
type MQTTDataCommand struct {
	Kind string          `json:"kind"`
	Type string          `json:"type,omitempty"`
	Data json.RawMessage `json:"data"`
}

// MQTT data delivery types and statuses for the privacy envelope flow.
const (
	// MQTTDataTypePrivacy marks an envelope whose Data block must be fetched
	// from the backend instead of read inline — keeps secrets off the broker.
	MQTTDataTypePrivacy = "privacy"
	// MQTTStatusReceived is the ack the device publishes the moment it accepts
	// a privacy envelope, before the async backend fetch begins. Tells the
	// backend "got it, stop retrying" without being the terminal status.
	MQTTStatusReceived = "received"
)

// MQTTDataResponse is the fd_channel reply for cmd:"data".
// Echoes Kind so the server can correlate with its outbound request.
type MQTTDataResponse struct {
	MQTTInfoResponse
	Kind   string      `json:"kind"`
	Status string      `json:"status"`
	Error  string      `json:"error,omitempty"`
	Data   interface{} `json:"data,omitempty"`
}

// MQTTSystemInfoData is the response payload for kind:"system.info" — an
// aggregate snapshot of versions + network + host. Fields are zero-valued when
// the probe fails (e.g. openclaw not yet installed → OpenClaw="", OpenClawDetected=false).
type MQTTSystemInfoData struct {
	Versions MQTTVersionsData `json:"versions"`
	Network  MQTTNetworkData  `json:"network"`
	Host     MQTTHostData     `json:"host"`
}

// MQTTVersionsData carries the component version strings on the device.
// Empty string means probing failed; OpenClawDetected lets the caller
// distinguish "not installed" from "installed but unparseable".
type MQTTVersionsData struct {
	OSServer         string `json:"os-server"`
	Bootstrap        string `json:"bootstrap"`
	Hal              string `json:"hal"`
	OpenClaw         string `json:"openclaw"`
	OpenClawDetected bool   `json:"openclaw_detected"`
}

// MQTTNetworkData carries link facts for the interface holding the default route
// (Interface names it — wlan0 on WiFi, eth0/end0 on ethernet). SSID is empty when
// the device is in AP mode, wired, or otherwise not joined to upstream Wi-Fi.
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
	Timezone      string `json:"timezone,omitempty"` // active IANA zone, live from the device
}

// MQTTOAuthSetData is the Data payload for kind:"oauth.set".
// Provider is a free-form key (e.g. "google", "twitter", "github") used as the
// map key in access_tokens.json.
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
// Channel selects which channels.<channel> block to re-apply. Today only "slack"
// is implemented; other channels return an error. Credentials are read from
// config.json on the device — they are NOT carried in this payload.
type MQTTChannelRefreshConfigData struct {
	Channel string `json:"channel"`
}

// MQTTChannelRefreshConfigResultData is the Data payload echoed in fd_channel
// success/failure messages for kind:"channel.refresh_config". Runtime carries
// the detected openclaw runtime version string (empty if probing failed) so the
// backend can correlate refresh outcomes with runtime upgrades.
type MQTTChannelRefreshConfigResultData struct {
	Channel string `json:"channel"`
	Runtime string `json:"runtime,omitempty"`
}

// OAuthTokenEntry is the on-disk representation of a single provider's token
// inside access_tokens.json.
type OAuthTokenEntry struct {
	AccessToken    string   `json:"access_token"`
	RefreshToken   string   `json:"refresh_token,omitempty"`
	TokenType      string   `json:"token_type,omitempty"`
	ExpiresAt      int64    `json:"expires_at,omitempty"`
	Scopes         []string `json:"scopes,omitempty"`
	UserEmail      string   `json:"user_email,omitempty"`
	ClientID       string   `json:"client_id,omitempty"`
	ObtainedAt     int64    `json:"obtained_at"`               // unix seconds when this device received the token
	RefreshRevoked bool     `json:"refresh_revoked,omitempty"` // set when the backend returned invalid_grant — skip until user re-auths
}

// AccessTokensFile is the on-disk schema for workspace/configs/access_tokens.json.
type AccessTokensFile struct {
	Version   int                        `json:"version"`
	Providers map[string]OAuthTokenEntry `json:"providers"`
}

// MQTTConnectorSetData is the Data payload for kind:"connector.set.<code>".
// The backend drives the OAuth/app flow and pushes the resulting credentials
// here; the device writes the token file + the mcp.servers.<code> entry into
// openclaw.json. ExpiresIn (seconds-from-now) is normalized to an absolute
// ExpiresAt on store.
type MQTTConnectorSetData struct {
	Connector    string `json:"connector"`
	AuthType     string `json:"auth_type"`
	AccessToken  string `json:"access_token,omitempty"`
	RefreshToken string `json:"refresh_token,omitempty"`
	TokenType    string `json:"token_type,omitempty"`
	ExpiresIn    int    `json:"expires_in,omitempty"` // seconds from now
	ExpiresAt    int64  `json:"expires_at,omitempty"` // unix seconds (wins over expires_in)
	// APIKey carries the credential for static-API-key connectors (e.g. Ahrefs)
	// whose auth_type is not OAuth — the key lands here, not in access_token.
	APIKey    string   `json:"api_key,omitempty"`
	Scopes    []string `json:"scopes,omitempty"`
	UserEmail string   `json:"user_email,omitempty"`
	ClientID  string   `json:"client_id,omitempty"`
	// Credentials holds connector-specific extras the backend wants persisted
	// verbatim (preserved across token refreshes).
	Credentials map[string]string `json:"credentials,omitempty"`
	// Refresh gates the connector refresh loop: only entries with refresh:true
	// AND a refresh_token are auto-rotated. Backend is the source of truth.
	Refresh bool `json:"refresh,omitempty"`
}

// MQTTConnectorRemoveData is the Data payload for kind:"connector.remove.<code>".
type MQTTConnectorRemoveData struct {
	Connector string `json:"connector"`
}

// ConnectorEntry is the on-disk representation of a single connector's
// credentials inside workspace/configs/connectors.json.
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

// MQTTSkillsInstallData is the Data payload for kind:"skills.install".
// Role is a free-form slug owned by the backend catalog; the device fetches
// <role>/skills.zip on demand.
type MQTTSkillsInstallData struct {
	Role string `json:"role"`
}

// MQTTSoftwareUpdateData is the data block of system.software_update. Target is
// one of os-server | bootstrap | web | hal | device | <agent CLI key> | agent
// ("agent" resolves to the configured runtime's CLI).
type MQTTSoftwareUpdateData struct {
	Target string `json:"target"`
}

// MQTTSkillsSaveData is the Data payload for kind:"skills.save" — an authored
// skill pushed from the backend instead of the web UI's form. Same three fields
// as SkillDraft (which this maps onto); Name must be a slug matching
// ^[a-z0-9_-]+$, enforced device-side by skills.ValidateSkillName.
type MQTTSkillsSaveData struct {
	Name         string `json:"name"`
	Description  string `json:"description"`
	Instructions string `json:"instructions"`
}

// MQTTSkillsUploadData is the Data payload for kind:"skills.upload". Filename
// selects the accepted .md, .zip, or .skill path; ContentBase64 is the exact
// file bytes. The decoded file is capped at skills.StoreMaxBytes.
type MQTTSkillsUploadData struct {
	Filename      string `json:"filename"`
	ContentBase64 string `json:"content_base64"`
}

// MQTTSkillsFilesData is the Data payload for kind:"skills.files".
//
//	{"name":"music"}                        → file list, no contents
//	{"name":"music","path":"music/SKILL.md"} → that one file, contents inlined
//
// Path is the entry path exactly as the list reported it (relative to the skills
// root, so it includes the skill dir).
type MQTTSkillsFilesData struct {
	Name string `json:"name"`
	Path string `json:"path"`
}

// InboundFile is a file the USER attached to a turn, carried inbound so the
// agent's tools can open it. Separate from the `image` field, which stays for
// photos: an image goes through the describe-first vision gate, a document must
// not (a PDF fed to a vision model fails, and used to land as `.jpg` besides).
type InboundFile struct {
	// Name is the client's filename. Used ONLY for its extension — the path
	// written on disk is generated, so a hostile name can't steer the write.
	Name string `json:"name"`
	// MIME is advisory, for a client that wants to label the attachment. The
	// device does not trust it to decide anything.
	MIME string `json:"mime,omitempty"`
	// Content is the file base64-encoded, capped at agentfile.InboundMaxBytes.
	Content string `json:"content"`
}

// MQTTChatSendData is the Data payload for kind:"chat.send".
type MQTTChatSendData struct {
	Message string `json:"message"`
	// Files are optional non-image attachments (PDF, CSV, …). Use Images for
	// photos — that path runs the vision gate, this one does not.
	Files []InboundFile `json:"files,omitempty"`
	// Images are optional base64 JPEGs, exactly what the web chat puts in the
	// sensing event's `images` field — so a phone can attach photos the same
	// way, several in one message.
	Images []string `json:"images,omitempty"`
	// SessionID is opaque to the device: it is echoed on the ack and on every
	// chat.event of this run so the backend can fan the stream back out to the
	// right client. The device does NOT partition conversation state by it —
	// there is one agent and one history, the same as standing next to the box.
	SessionID string `json:"session_id,omitempty"`
	// Speak makes the device say the reply out loud as well. Off by default: a
	// phone user chatting from another room does not expect the device to start
	// talking, which is also why the web chat suppresses TTS.
	Speak bool `json:"speak,omitempty"`
}

// MQTTChatSendResult is the Data block of the chat.send ack. The stream of
// chat.event messages that follows carries the same RunID.
type MQTTChatSendResult struct {
	RunID     string `json:"run_id"`
	SessionID string `json:"session_id,omitempty"`
}

// MQTTChatEventData is the Data payload for kind:"chat.event" — one monitor
// event of an in-flight chat.send run.
type MQTTChatEventData struct {
	RunID     string       `json:"run_id"`
	SessionID string       `json:"session_id,omitempty"`
	Event     MonitorEvent `json:"event"`
}

// MQTTChatFileGetData is the Data payload for kind:"chat.file.get" — a request
// for ONE device-local file the client found named in a message it is rendering.
type MQTTChatFileGetData struct {
	// Path is the device path, exactly as it appeared in the turn. Treated as
	// hostile input and validated against the agentfile allow-list.
	Path string `json:"path"`
	// SessionID and RunID are opaque to the device and echoed back untouched, so
	// the backend can route the reply to the client that asked. Both optional:
	// a file can be requested long after its run is over.
	SessionID string `json:"session_id,omitempty"`
	RunID     string `json:"run_id,omitempty"`
}

// MQTTChatFileData is the Data block of a chat.file.get reply — one file's
// metadata plus, when it fits, its bytes.
type MQTTChatFileData struct {
	RunID     string `json:"run_id,omitempty"`
	SessionID string `json:"session_id,omitempty"`
	// Name is the basename; Path echoes what was asked for, so a reply can be
	// matched to its request without relying on ordering.
	Name string `json:"name"`
	Path string `json:"path"`
	MIME string `json:"mime"`
	Size int64  `json:"size"`
	// Content is the file base64-encoded — the mirror image of how a chat.send
	// carries an inbound image, so the backend handles one encoding in both
	// directions. Empty when TooLarge is set.
	Content string `json:"content,omitempty"`
	// TooLarge marks a file past the MQTT inline budget: the metadata still
	// comes back so a client can say "a 12 MB video" instead of showing nothing,
	// but the bytes are not on the wire.
	TooLarge bool `json:"too_large,omitempty"`
}

// MQTTSkillsUninstallData is the Data payload for kind:"skills.uninstall".
type MQTTSkillsUninstallData struct {
	Name string `json:"name"`
}

// MQTTSkillsInstallStoreData is the Data payload for kind:"skills.install_store" — ONE skill
// from the Autonomous skill catalog, identified by its catalog id.
type MQTTSkillsInstallStoreData struct {
	ID string `json:"id"`
	// Name is an optional fallback, used only when the archive has no single
	// wrapping directory to take the skill name from.
	Name string `json:"name"`
}

// MQTTTTSSetData is the nested data payload for cmd:"data", kind:"tts.set" downlinks.
// BFF sends: {"cmd":"data","kind":"tts.set","data":{"provider":"elevenlabs","voice":"Linh","language":"vi"}}
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

// MQTTTTSSetAck is published to fd_channel after applying (or failing) a tts.set downlink.
// status: "starting" | "success" | "failure"
type MQTTTTSSetAck struct {
	MQTTInfoResponse
	Kind   string          `json:"kind"`
	Status string          `json:"status"`
	Error  string          `json:"error,omitempty"`
	Data   *MQTTTTSSetData `json:"data,omitempty"`
}

// ===========================================================================
// Configure the realtime voice agent (Gemini Live / OpenAI Realtime) from the
// backend / web dashboard. The payload (RealtimeSetData) is shared by TWO
// transports — pick whichever fits:
//   • MQTT  — `realtime.set` downlink (envelope below). Async ack on fd_channel.
//   • HTTP  — POST the device config endpoint with a `"realtime"` object holding
//             the SAME fields: {"realtime": { ...RealtimeSetData... }} (rides the
//             existing UpdateConfig route, exactly like tts_provider/stt_language).
// Both paths validate, write the `realtime` block to config.json, and restart HAL.
//
// HOW TO PUSH over MQTT (for FE / BFF teams)
// --------------------------------
// Publish a DOWNLINK to the device's command channel (the same `fa_channel`
// topic tts.set uses — i.e. the topic the device subscribes to). Envelope:
//
//	{ "cmd": "data", "kind": "realtime.set", "data": { ...RealtimeSetData... } }
//
// The device replies on its `fd_channel` (uplink) with MQTTRealtimeSetAck:
//   1) {"kind":"realtime.set","status":"starting"}            — received
//   2) {"kind":"realtime.set","status":"success","data":{…}}  — applied, OR
//      {"kind":"realtime.set","status":"failure","error":"…"} — rejected
//
// EFFECT: the device writes the values into the `realtime` block of its
// config.json and RESTARTS HAL (takes a few seconds). HAL then reads the new
// block on boot. So `success` means "saved + hal restarting", not "live yet".
//
// FIELD SEMANTICS (all fields optional; omit a field = leave it unchanged)
//   enabled   bool   — turn the realtime brain on/off. false = off (device
//                      falls back to the classic STT→agent→TTS path).
//   provider  string — "gemini" | "openai" | "none". "none" = off.
//   model     string — applied to the ACTIVE provider (the one in `provider`,
//                      or the current provider if `provider` is omitted).
//   voice     string — active provider's voice. Valid:
//                        gemini: Puck | Charon | Kore | Fenrir | Aoede
//                        openai: alloy | ash | coral | echo | fable | onyx |
//                                nova | sage | shimmer
//   reasoning string — active provider's reasoning depth (cost knob). Valid:
//                        gemini (thinking_level): MINIMAL | LOW | MEDIUM | HIGH
//                        openai (reasoning_effort): minimal|low|medium|high|xhigh
//                      Defaults are the CHEAPEST tier (MINIMAL / minimal).
//   api_key   string — override the realtime provider key. Empty/omitted →
//                      falls back to the device's llm_api_key.
//   base_url  string — override the realtime endpoint. Empty/omitted → derived
//                      from llm_base_url (…/ws/gemini or …/ws/openai).
//
// RULES
//   - When any of model/voice/reasoning is sent, `provider` (or the current
//     provider) MUST be a concrete gemini|openai — those knobs are per-provider.
//   - Invalid provider/voice/reasoning → status:"failure" with a descriptive
//     error; nothing is written.
//
// EXAMPLES
//   Switch to OpenAI Realtime with a voice:
//     {"cmd":"data","kind":"realtime.set","data":{"provider":"openai","voice":"alloy"}}
//   Tune Gemini reasoning up (more expensive):
//     {"cmd":"data","kind":"realtime.set","data":{"provider":"gemini","reasoning":"HIGH"}}
//   Turn realtime off:
//     {"cmd":"data","kind":"realtime.set","data":{"enabled":false}}
// ===========================================================================

// RealtimeSetData is the realtime-config payload, shared by the MQTT
// `realtime.set` downlink (data block) and the HTTP UpdateConfig `realtime` field.
type RealtimeSetData struct {
	Enabled   *bool  `json:"enabled,omitempty"`   // nil = leave unchanged
	Provider  string `json:"provider,omitempty"`  // gemini | openai | gptlive | pipecat_v1 | none
	Model     string `json:"model,omitempty"`     // active provider's model
	Voice     string `json:"voice,omitempty"`     // active provider's voice
	Reasoning string `json:"reasoning,omitempty"` // gemini thinking_level OR openai reasoning_effort (gptlive / pipecat_v1: none)
	APIKey    string `json:"api_key,omitempty"`   // optional override; empty → llm_api_key
	BaseURL   string `json:"base_url,omitempty"`  // optional override; empty → llm_base_url-derived
	// WebSearch toggles the in-session `web_search` tool (pipecat_v1 only —
	// rejected for any other provider). nil = leave unchanged.
	WebSearch *bool `json:"web_search,omitempty"`
}

// MQTTRealtimeSetCommand wraps the full realtime.set downlink envelope for unmarshalling.
type MQTTRealtimeSetCommand struct {
	Data RealtimeSetData `json:"data"`
}

// MQTTRealtimeSetAck is published to fd_channel after applying (or failing) a
// realtime.set downlink. status: "starting" | "success" | "failure".
type MQTTRealtimeSetAck struct {
	MQTTInfoResponse
	Kind   string           `json:"kind"`
	Status string           `json:"status"`
	Error  string           `json:"error,omitempty"`
	Data   *RealtimeSetData `json:"data,omitempty"`
}

// WakeWordGateData controls the top-level wakeword config flag. Enabled is a
// pointer so an omitted field can be rejected instead of silently disabling the
// gate.
//
//	{ "cmd": "data", "kind": "wakeword.gate", "data": { "enabled": true } }
type WakeWordGateData struct {
	Enabled *bool `json:"enabled" validate:"required"`
}

// MQTTWakeWordGateAck is published to fd_channel after applying (or failing) a
// wakeword.gate downlink. status: "starting" | "success" | "failure".
type MQTTWakeWordGateAck struct {
	MQTTInfoResponse
	Kind   string            `json:"kind"`
	Status string            `json:"status"`
	Error  string            `json:"error,omitempty"`
	Data   *WakeWordGateData `json:"data,omitempty"`
}

// AgentRuntimeSetData carries the target backend for a runtime switch. The MQTT
// path no longer reads Runtime off the wire — the hermes.setup / picoclaw.setup
// kind names the target — but it is still the request body for the HTTP
// POST /api/device/agent-runtime path and the value persisted to config. The
// valid set mirrors agent/factory.go's resolver; anything else is rejected (we
// don't silently fall back here — an unknown value from the BFF is a contract
// error, not a default).
//
//	{ "cmd": "data", "kind": "hermes.setup" }      // switch to hermes
//	{ "cmd": "data", "kind": "picoclaw.setup" }    // switch to picoclaw
//	{ "cmd": "data", "kind": "claudecode.setup" }  // switch to claude code
//	{ "cmd": "data", "kind": "openclaw.setup" }    // revert to openclaw (baseline)
type AgentRuntimeSetData struct {
	Runtime string `json:"runtime"` // "openclaw" | "hermes" | "picoclaw" | "claudecode" | "remote"
	// URL and Token are only read when Runtime == "remote": they configure the
	// external gateway the device forwards user turns to. Ignored (and blank on
	// the wire) for every other runtime. See AgentRuntimeRemote for the Phase-A
	// caveat — the value is persisted but no real switch happens yet.
	URL   string `json:"url,omitempty"`
	Token string `json:"token,omitempty"`
}

// AgentRuntimes is the valid set, surfaced to the web settings dropdown via
// GET /api/device/agent-runtime so the UI never hardcodes the list.
var AgentRuntimes = []string{AgentRuntimeOpenClaw, AgentRuntimeHermes, AgentRuntimePicoclaw, AgentRuntimeCodex, AgentRuntimeClaudeCode, AgentRuntimeOpenCode, AgentRuntimeRemote}

// IsValidAgentRuntime reports whether r is a switchable backend (case-insensitive,
// trimmed). Used to validate hermes.setup / picoclaw.setup and the HTTP runtime
// switch before any side effects.
func IsValidAgentRuntime(r string) bool {
	r = strings.ToLower(strings.TrimSpace(r))
	for _, v := range AgentRuntimes {
		if v == r {
			return true
		}
	}
	return false
}

// AgentRuntimeStatus is returned by GET /api/device/agent-runtime: the active
// backend plus the selectable options.
type AgentRuntimeStatus struct {
	Current string   `json:"current"`
	Options []string `json:"options"`

	// Ready reports whether the backend is actually answering, not merely
	// selected. config.agent_runtime flips as soon as the switch lands, but the
	// gateway behind it may still be booting — hermes downloads nothing but
	// still takes tens of seconds to come up. A UI that calls that "active"
	// invites the operator to start a turn against a backend that is not
	// listening yet, which reads as a broken device.
	Ready bool `json:"ready"`

	// RemoteURL and RemoteToken echo the stored remote-gateway config so the
	// web Runtime page can pre-fill the fields when the user re-opens it.
	// Blank on every runtime other than "remote"; the token is returned as-is
	// (no redaction) because the settings page is behind adminAuthMiddleware.
	RemoteURL   string `json:"remote_url,omitempty"`
	RemoteToken string `json:"remote_token,omitempty"`
}

// TimezoneStatus is returned by GET /api/device/timezone: the device's active
// IANA zone plus the selectable list (from the system tzdata), so the web picker
// never hardcodes the zone list.
type TimezoneStatus struct {
	Current string   `json:"current"`
	Zones   []string `json:"zones"`
}

// TimezoneSetData is the IANA zone name to apply (e.g. "Asia/Ho_Chi_Minh"),
// shared by the HTTP POST /api/device/timezone body and the MQTT `timezone.set`
// downlink data block. Invalid/unknown zones are rejected.
//
//	{ "cmd": "data", "kind": "timezone.set", "data": { "timezone": "Asia/Ho_Chi_Minh" } }
type TimezoneSetData struct {
	Timezone string `json:"timezone" validate:"required"`
}

// MQTTTimezoneSetAck is published to fd_channel after applying (or failing) a
// timezone.set downlink. status: "starting" | "success" | "failure". Mirrors
// MQTTRealtimeSetAck.
type MQTTTimezoneSetAck struct {
	MQTTInfoResponse
	Kind   string           `json:"kind"`
	Status string           `json:"status"`
	Error  string           `json:"error,omitempty"`
	Data   *TimezoneSetData `json:"data,omitempty"`
}

// AgentRuntimeSetAck is published to fd_channel after applying (or failing) a
// hermes.setup / picoclaw.setup downlink — Kind echoes the triggering kind so
// the worker can match it. status: "starting" | "success" | "failure". On
// "success" the device restarts os-server, so the BFF should expect a brief
// reconnect — the new banner (AGENT BACKEND ACTIVE) confirms the swap landed.
type AgentRuntimeSetAck struct {
	MQTTInfoResponse
	Kind   string               `json:"kind"`
	Status string               `json:"status"`
	Error  string               `json:"error,omitempty"`
	Data   *AgentRuntimeSetData `json:"data,omitempty"`
}

// MQTTTTSPreviewData is the nested data payload for cmd:"data", kind:"tts.preview".
// Text is required; Speed is an optional per-utterance rate (0.25–4.0).
// Provider/Voice/Language are optional overrides — empty
// fields make HAL fall back to the device's current TTS config.
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

// MQTTDeviceRenameData is the nested data payload for cmd:"data", kind:"device.rename".
// Name is the new agent name written into workspace/IDENTITY.md's **Name:** line.
// WatchIdentity picks up the change within 5s and pushes new wake words to HAL;
// OpenClaw re-reads IDENTITY.md on its own — no gateway restart needed.
type MQTTDeviceRenameData struct {
	Name string `json:"name"`
}

// ConfigPublicResponse is returned by GET /api/device/config. Raw secrets
// (API keys, channel tokens, MQTT/WiFi passwords) are replaced by boolean
// presence flags so the web UI can render "configured ✓" + a write-only
// SecretUpdateField. Non-secret fields (URLs, IDs, model name, language)
// are returned as-is because they're useful for the UI and not sensitive.
// RealtimePublic is the read-back view of the realtime voice-agent config — the
// RESOLVED active-provider values (provider/model/voice/reasoning for whichever
// provider is active, enabled state, resolved base_url). Mirrors how
// tts_provider/tts_voice are surfaced; the key is exposed only as HasAPIKey.
type RealtimePublic struct {
	Enabled   bool   `json:"enabled"`
	Provider  string `json:"provider"` // "" when realtime is off
	Model     string `json:"model"`
	Voice     string `json:"voice"`
	Reasoning string `json:"reasoning"`
	BaseURL   string `json:"base_url"` // resolved (may be llm-derived)
	HasAPIKey bool   `json:"has_api_key"`
	// WebSearch is the resolved in-session web-search toggle: present only for
	// pipecat_v1 (the provider that has the knob), omitted otherwise.
	WebSearch *bool `json:"web_search,omitempty"`
}

type ConfigPublicResponse struct {
	Environment EnvironmentConfig `json:"environment"`

	Channel            string   `json:"channel"`
	TelegramUserID     string   `json:"telegram_user_id"`
	SlackUserID        string   `json:"slack_user_id"`
	DiscordGuildID     string   `json:"discord_guild_id"`
	DiscordUserID      string   `json:"discord_user_id"`
	WhatsappUserID     string   `json:"whatsapp_user_id"`
	LLMModel           string   `json:"llm_model"`
	LLMBaseURL         string   `json:"llm_base_url"`
	LLMDisableThinking bool     `json:"llm_disable_thinking"`
	STTBaseURL         string   `json:"stt_base_url"`
	TTSBaseURL         string   `json:"tts_base_url"`
	STTLanguage        string   `json:"stt_language"`
	STTModel           string   `json:"stt_model"`
	TTSProvider        string   `json:"tts_provider"`
	TTSVoice           string   `json:"tts_voice"`
	TTSSpeed           float64  `json:"tts_speed"`
	WakeWord           bool     `json:"wakeword"`
	AgentName          string   `json:"agent_name"`
	WakePhrases        []string `json:"wake_phrases"`
	DeviceID           string   `json:"device_id"`
	Mac                string   `json:"mac"`
	NetworkSSID        string   `json:"network_ssid"`
	MQTTEndpoint       string   `json:"mqtt_endpoint"`
	MQTTUsername       string   `json:"mqtt_username"`
	MQTTPort           int      `json:"mqtt_port"`
	FAChannel          string   `json:"fa_channel"`
	FDChannel          string   `json:"fd_channel"`

	// Realtime voice-agent config — RESOLVED active-provider values for the web to
	// render the form. Write back via UpdateConfig's `realtime` field. The api_key
	// is never returned (only HasAPIKey, the realtime-specific override; the LLM
	// key fallback is reported by HasLLMAPIKey).
	Realtime RealtimePublic `json:"realtime"`

	// Presence booleans replace raw secret values. Frontend renders
	// "configured · update" affordance when true, empty input when false.
	HasTelegramBotToken bool `json:"has_telegram_bot_token"`
	HasSlackBotToken    bool `json:"has_slack_bot_token"`
	HasSlackAppToken    bool `json:"has_slack_app_token"`
	HasDiscordBotToken  bool `json:"has_discord_bot_token"`
	HasLLMAPIKey        bool `json:"has_llm_api_key"`
	HasDeepgramAPIKey   bool `json:"has_deepgram_api_key"`
	HasSTTAPIKey        bool `json:"has_stt_api_key"`
	HasTTSAPIKey        bool `json:"has_tts_api_key"`
	HasNetworkPassword  bool `json:"has_network_password"`
	HasMQTTPassword     bool `json:"has_mqtt_password"`
	HasAdminPassword    bool `json:"has_admin_password"`

	// True once the shipped credential set has been preserved. Drives the
	// "restore Autonomous default" affordance — there is nothing to offer on a
	// device that has never had an operator edit, because nothing was replaced.
	HasAutonomousDefaults bool `json:"has_autonomous_defaults"`

	// The non-secret half of the stored set. The web compares these against the
	// live values to tell whether the device is still on the Autonomous brain or
	// on one the operator supplied — a mode it shows as a dropdown. The key is
	// never returned, here or anywhere else.
	AutonomousDefaultBaseURL string `json:"autonomous_default_base_url,omitempty"`
	AutonomousDefaultModel   string `json:"autonomous_default_model,omitempty"`
}

// UpdateConfigRequest is used by PUT /api/device/config to update device settings.
// All fields are optional; only non-empty values are applied.
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

	LLMBaseURL         string `json:"llm_base_url"`
	LLMAPIKey          string `json:"llm_api_key"`
	LLMModel           string `json:"llm_model"`
	LLMDisableThinking *bool  `json:"llm_disable_thinking,omitempty"`

	DeepgramAPIKey string `json:"deepgram_api_key"`
	STTAPIKey      string `json:"stt_api_key"`
	TTSAPIKey      string `json:"tts_api_key"`
	// ClearTTSAPIKey deletes the stored TTS key. An empty TTSAPIKey cannot say
	// this: every field here is PATCH-style, where "" means "not sent". The
	// settings page sets it when the operator switches TTS provider, so the
	// previous vendor's credential can't be handed to the new one.
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

	// Realtime voice-agent config (Gemini Live / OpenAI Realtime). Same payload
	// as the MQTT realtime.set downlink; omit to leave the realtime block alone.
	// See RealtimeSetData for field semantics + valid values.
	Realtime *RealtimeSetData `json:"realtime,omitempty"`

	// AdminPassword rotates the bcrypt hash when non-empty. Existing sessions
	// keep working (they ride config.SessionSecret, not the hash); to nuke
	// every outstanding session the operator must rotate SessionSecret too.
	AdminPassword string `json:"admin_password"`
}

// TTS provider constants.
const (
	TTSProviderOpenAI     = "openai"
	TTSProviderElevenLabs = "elevenlabs"
	// TTSProviderPiper synthesises on the device. No base URL and no API key
	// apply, and there is no shared rate limit to queue behind — every unit
	// renders its own audio.
	TTSProviderPiper = "piper"
	// TTSProviderGemini renders with Gemini TTS models through the autonomous
	// proxy's Gemini REST relay (HAL hal/drivers/voice/tts/gemini.py).
	TTSProviderGemini = "gemini"
)

// DefaultGeminiVoice is seeded when a device defaults to gemini without a
// voice. Gemini prebuilt voices are multilingual, so it is not language-aware.
// Must stay in HAL's GeminiTTSBackend.VOICES.
const DefaultGeminiVoice = "Kore"

// TTSProviders is the list of supported TTS providers.
var TTSProviders = []string{TTSProviderOpenAI, TTSProviderElevenLabs, TTSProviderPiper, TTSProviderGemini}

// IsValidTTSProvider reports whether p is a supported TTS provider. Used to
// reject a bad ROBOT.md `voice.tts_provider` before seeding it into config.
func IsValidTTSProvider(p string) bool {
	for _, v := range TTSProviders {
		if v == p {
			return true
		}
	}
	return false
}

// DefaultElevenLabsVoiceForLang returns the ElevenLabs voice os-server seeds when
// a device defaults to the elevenlabs provider (via ROBOT.md voice.tts_provider)
// but declares no explicit voice. Language-aware so a VN/CN owner boots with a
// voice trained on their language instead of an American one. The names must
// stay in sync with the top picks (*) in HAL's elevenlabs.py VOICE_IDS_BY_LANG:
// vi→Ngan, zh(-CN/-TW)→Amy, everything else→Rachel. Prefix match mirrors that
// module's voices_for_language bucket logic.
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
	// Piper voices are files on the device, so the live list comes from HAL
	// (which enumerates the installed .onnx models). Empty on purpose: no image
	// ships a Piper voice — they are downloaded on request — so there is no
	// name that is safe to offer when HAL is unreachable. Naming one anyway
	// gets it saved as the configured voice, and the device is then set to a
	// model it does not have. An empty list makes the UI say so instead.
	TTSProviderPiper: {},
	// Mirrors HAL's GeminiTTSBackend.VOICES (the live list comes from HAL).
	TTSProviderGemini:     {"Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat"},
	TTSProviderElevenLabs: {"Rachel", "Sarah", "Grace", "Freya", "Matilda", "Emily", "Alice", "Lily", "Charlotte", "Nicole", "Glinda", "Serena", "Jessie", "Brian", "Adam", "Daniel", "George", "James", "Liam", "Callum", "Harry", "Charlie", "Chris", "Sam"},
}

// TTSVoices is the default (OpenAI) voice list for backward compatibility.
var TTSVoices = TTSVoicesByProvider[TTSProviderOpenAI]

// DefaultTTSVoice is the default voice when none is configured.
const DefaultTTSVoice = "alloy"
