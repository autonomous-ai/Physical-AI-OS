package device

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"

	"golang.org/x/crypto/bcrypt"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/i18n"
	"go.autonomous.ai/os/system/lib/urlnorm"
	"go.autonomous.ai/os/system/server/config"
)

// GetPublicConfig returns the device configuration with secrets replaced by
// presence booleans, suitable for browser bootstrap. The web UI renders
// write-only fields against the `Has*` flags so plaintext tokens never reach
// the DOM / sessionStorage / HAR captures.
func (s *Service) GetPublicConfig() domain.ConfigPublicResponse {
	disableThinking := false
	if s.config.LLMDisableThinking != nil {
		disableThinking = *s.config.LLMDisableThinking
	}
	deviceID := s.config.DeviceID
	if deviceID == "" {
		deviceID = GetDeviceMac()
	}
	agentName := i18n.DeviceName()
	deviceType := s.config.DeviceTypeOrDefault()
	return domain.ConfigPublicResponse{
		Environment:        s.config.EnvironmentSettings(),
		Channel:            s.config.Channel,
		TelegramUserID:     s.config.TelegramUserID,
		SlackUserID:        s.config.SlackUserID,
		DiscordGuildID:     s.config.DiscordGuildID,
		DiscordUserID:      s.config.DiscordUserID,
		WhatsappUserID:     s.config.WhatsappUserID,
		BluebubblesServerURL:   s.config.BluebubblesServerURL,
		BluebubblesUserAddress: s.config.BluebubblesUserAddress,
		BluebubblesCallerContext: s.config.BluebubblesCallerContext,
		LLMModel:           s.config.LLMModel,
		LLMBaseURL:         s.config.LLMBaseURL,
		LLMDisableThinking: disableThinking,
		STTBaseURL:         s.config.STTBaseURL,
		TTSBaseURL:         s.config.TTSBaseURL,
		STTLanguage:        s.config.STTLanguage,
		STTModel:           s.config.STTModel,
		TTSProvider:        s.config.TTSProvider,
		TTSVoice:           s.config.TTSVoice,
		TTSSpeed:           s.config.GetTTSSpeed(),
		WakeWord:           s.config.WakeWordEnabled(),
		AgentName:          agentName,
		WakePhrases:        i18n.BuildSupportedVoiceWakeWords(agentName, deviceType),
		DeviceID:           deviceID,
		Mac:                GetDeviceMac(),
		NetworkSSID:        s.config.NetworkSSID,
		MQTTEndpoint:       s.config.MQTTEndpoint,
		MQTTUsername:       s.config.MQTTUsername,
		MQTTPort:           s.config.MQTTPort,
		FAChannel:          s.config.FAChannel,
		FDChannel:          s.config.FDChannel,

		HasTelegramBotToken:      s.config.TelegramBotToken != "",
		HasSlackBotToken:         s.config.SlackBotToken != "",
		HasSlackAppToken:         s.config.SlackAppToken != "",
		HasDiscordBotToken:       s.config.DiscordBotToken != "",
		HasBluebubblesPassword:   s.config.BluebubblesPassword != "",
		HasLLMAPIKey:             s.config.LLMAPIKey != "",
		HasDeepgramAPIKey:        s.config.DeepgramAPIKey != "",
		HasSTTAPIKey:             s.config.STTAPIKey != "",
		HasTTSAPIKey:             s.config.TTSAPIKey != "",
		HasAutonomousDefaults:    s.config.AutonomousDefaults != nil,
		AutonomousDefaultBaseURL: autonomousDefaultBaseURL(s.config),
		AutonomousDefaultModel:   autonomousDefaultModel(s.config),
		HasNetworkPassword:       s.config.NetworkPassword != "",
		HasMQTTPassword:          s.config.MQTTPassword != "",
		HasAdminPassword:         s.config.AdminPasswordHash != "",
		Realtime: domain.RealtimePublic{
			Enabled:   s.config.RealtimeEnabled(),
			Provider:  s.config.RealtimeProvider(),
			Model:     s.config.RealtimeModel(),
			Voice:     s.config.RealtimeVoice(),
			Reasoning: s.config.RealtimeReasoning(),
			// Expose ONLY the explicit override (blank when deriving) so the web
			// form's "leave blank to derive" works and does not re-persist a bare
			// URL that breaks HAL's /ws/gemini handshake. See RealtimeBaseURL doc.
			BaseURL:   s.config.RealtimeBaseURLOverride(),
			HasAPIKey: s.config.RealtimeHasAPIKey(),
			WebSearch: s.config.RealtimeWebSearch(),
		},
	}
}

// VerifyAdminPassword returns nil when password matches the stored bcrypt hash.
// Returns an error when no password is set, when the hash is malformed, or when
// the password is wrong. Callers must not surface the specific error to clients
// (uniform "invalid credentials" message) to avoid leaking which case fired.
func (s *Service) VerifyAdminPassword(password string) error {
	if s.config.AdminPasswordHash == "" {
		return fmt.Errorf("admin password not configured")
	}
	return bcrypt.CompareHashAndPassword([]byte(s.config.AdminPasswordHash), []byte(password))
}

// updateChanges captures which field clusters an UpdateConfig save actually
// changed, plus the post-save values the side-effect blocks need. Computed
// entirely inside the WithLockSave closure (by applyUpdate) so the flags always
// describe the atomically-marshalled state, never a torn snapshot.
type updateChanges struct {
	model    bool // llm_model changed → sync primary into the gateway
	thinking bool // llm_disable_thinking sent → RefreshModelsConfig
	baseURL  bool // llm_base_url changed → RefreshModelsConfig
	apiKey   bool // llm_api_key rotated → RefreshModelsConfig (openclaw.json holds its OWN apiKey copy)
	wifi     bool // ssid changed → reconnect WiFi
	lang     bool // stt_language changed → new agent session + hal restart
	halBoot  bool // a field hal reads at boot changed → hal restart
	tts      bool // voice/provider changed → pushed into the running hal
	realtime bool // realtime block sent → hal restart
	channel  bool // messaging channel/tokens changed → re-push into gateway

	newModel    string
	newSSID     string
	newPassword string
	prevLang    string
	newLang     string
	chanReq     domain.AddChannelRequest
}

// bootSnapshot is the set of config fields hal reads at boot (hal/server.py
// :317-388 + hal/config.py:103-104) and has no way to be told about at
// runtime. Comparable, so a plain before/after equality check gates the hal
// restart — wifi/channel/MQTT/admin-only saves must not bounce TTS.
type bootSnapshot struct {
	llmAPIKey      string
	llmBaseURL     string
	deepgramAPIKey string
	sttAPIKey      string
	sttBaseURL     string
}

func bootFields(c *config.Config) bootSnapshot {
	return bootSnapshot{
		llmAPIKey:      c.LLMAPIKey,
		llmBaseURL:     c.LLMBaseURL,
		deepgramAPIKey: c.DeepgramAPIKey,
		sttAPIKey:      c.STTAPIKey,
		sttBaseURL:     c.STTBaseURL,
	}
}

// ttsSnapshot is the part hal can be told about while it runs, via POST
// /voice/tts/config. Kept separate from bootSnapshot because a voice change is
// the single most common save an operator makes, and restarting hal for it
// takes the device deaf and mute for ten to fifteen seconds — long enough that
// any admin click landing in the window is lost.
type ttsSnapshot struct {
	ttsProvider string
	ttsSpeed    float64
	ttsVoice    string
	ttsAPIKey   string
	ttsBaseURL  string
}

func ttsFields(c *config.Config) ttsSnapshot {
	return ttsSnapshot{
		ttsProvider: c.TTSProvider,
		ttsVoice:    c.TTSVoice,
		ttsSpeed:    c.GetTTSSpeed(),
		ttsAPIKey:   c.TTSAPIKey,
		ttsBaseURL:  c.TTSBaseURL,
	}
}

// realtimeFingerprint renders the realtime block as JSON so before and after
// can be compared. The struct holds pointers, which makes it uncomparable with
// ==, and this runs once per save, so serialising is the simpler answer.
func realtimeFingerprint(c *config.Config) string {
	if c.Realtime == nil {
		return ""
	}
	b, err := json.Marshal(c.Realtime)
	if err != nil {
		// Cannot happen for this struct; if it ever did, reporting "unchanged"
		// on both sides is the safe reading — the other flags still gate the
		// restart.
		return ""
	}
	return string(b)
}

// channelSnapshot is the messaging channel identity + tokens. Comparable; a
// before/after difference means the change only landed in config.json and must
// be re-pushed into the gateway (which keeps tokens in its OWN config).
type channelSnapshot struct {
	channel          string
	telegramBotToken string
	telegramUserID   string
	slackBotToken    string
	slackAppToken    string
	slackUserID      string
	discordBotToken  string
	discordGuildID   string
	discordUserID    string
	// iMessage / BlueBubbles — same before/after semantics: any change here
	// re-pushes the channel config into the active gateway (Hermes writes them
	// into ~/.hermes/.env; other runtimes reject the apply).
	bluebubblesServerURL   string
	bluebubblesPassword    string
	bluebubblesUserAddress string
	// Optional caller-context prompt — a change here still counts as a
	// channel change so presync re-writes BLUEBUBBLES_CALLER_CONTEXT.
	bluebubblesCallerContext string
}

func channelFields(c *config.Config) channelSnapshot {
	return channelSnapshot{
		channel:          c.Channel,
		telegramBotToken: c.TelegramBotToken,
		telegramUserID:   c.TelegramUserID,
		slackBotToken:    c.SlackBotToken,
		slackAppToken:    c.SlackAppToken,
		slackUserID:      c.SlackUserID,
		discordBotToken:  c.DiscordBotToken,
		discordGuildID:   c.DiscordGuildID,
		discordUserID:    c.DiscordUserID,
		bluebubblesServerURL:   c.BluebubblesServerURL,
		bluebubblesPassword:    c.BluebubblesPassword,
		bluebubblesUserAddress: c.BluebubblesUserAddress,
		bluebubblesCallerContext: c.BluebubblesCallerContext,
	}
}

// applyUpdate mutates c per the PATCH request and reports what changed. Pure
// config-in/config-out (no I/O, no locking) so the flag logic — the part of
// UpdateConfig where a mistake silently skips or duplicates a restart — is unit
// testable. Must run inside WithLockSave; adminHash is precomputed by the
// caller because bcrypt is too CPU-heavy for the lock.
func applyUpdate(c *config.Config, data domain.UpdateConfigRequest, adminHash string) updateChanges {
	var ch updateChanges
	prevBoot := bootFields(c)
	prevTTS := ttsFields(c)
	prevWakeWord := c.WakeWordEnabled()
	prevChannel := channelFields(c)
	ch.prevLang = c.STTLanguage

	// Before anything is overwritten. Ordering matters: the snapshot has to see
	// the values as they were, not as this save leaves them.
	if requestChangesCredentials(data) {
		captureAutonomousDefaults(c)
	}
	applyLLMFields(c, data, &ch)
	applyVoicePipelineFields(c, data, &ch)

	if data.DeviceID != "" {
		c.DeviceID = data.DeviceID
	}
	applyNetworkFields(c, data, &ch)
	applyChannelPatch(c, data)
	applyMQTTFields(c, data)
	if data.Environment != nil {
		settings := data.Environment.Clone()
		c.Environment = &settings
	}

	// Admin password rotation. Empty = keep existing hash; non-empty = bcrypt
	// + replace. Existing sessions stay valid (signed by SessionSecret), so
	// rotating the password alone won't lock the active operator out.
	if adminHash != "" {
		c.AdminPasswordHash = adminHash
	}

	ch.halBoot = bootFields(c) != prevBoot || c.WakeWordEnabled() != prevWakeWord
	ch.tts = ttsFields(c) != prevTTS
	ch.channel = channelFields(c) != prevChannel
	// Build the request from the post-save config (full current values, since
	// PATCH semantics mean a token the operator didn't touch keeps its value).
	ch.chanReq = domain.AddChannelRequest{
		Channel:          c.Channel,
		TelegramBotToken: c.TelegramBotToken, TelegramUserID: c.TelegramUserID,
		SlackBotToken: c.SlackBotToken, SlackAppToken: c.SlackAppToken, SlackUserID: c.SlackUserID,
		DiscordBotToken: c.DiscordBotToken, DiscordGuildID: c.DiscordGuildID, DiscordUserID: c.DiscordUserID,
		BluebubblesServerURL:   c.BluebubblesServerURL,
		BluebubblesPassword:    c.BluebubblesPassword,
		BluebubblesUserAddress: c.BluebubblesUserAddress,
		BluebubblesCallerContext: c.BluebubblesCallerContext,
	}
	return ch
}

// requestChangesCredentials reports whether this save carries a key or URL for
// any of the four credential surfaces. Model counts too: it is one of the three
// values the shipped set is made of.
func requestChangesCredentials(d domain.UpdateConfigRequest) bool {
	if d.LLMAPIKey != "" || d.LLMBaseURL != "" || d.LLMModel != "" ||
		d.TTSAPIKey != "" || d.TTSBaseURL != "" ||
		d.STTAPIKey != "" || d.STTBaseURL != "" {
		return true
	}
	return d.Realtime != nil && (d.Realtime.APIKey != "" || d.Realtime.BaseURL != "")
}

// captureAutonomousDefaults snapshots the shipped credentials before the first
// operator edit overwrites them. Runs at most once per device: the stored copy
// is what "restore" reads later, so re-capturing after an edit would preserve
// the operator's own values under the Autonomous name and lose the real ones
// for good — which is exactly the state devices were reaching before this.
//
// Skips a device with nothing to preserve (a config that never had credentials),
// so an empty set is never mistaken for a valid default.
func captureAutonomousDefaults(c *config.Config) {
	if c.AutonomousDefaults != nil || c.LLMAPIKey == "" || c.LLMBaseURL == "" {
		return
	}
	c.AutonomousDefaults = &config.AutonomousDefaults{
		BaseURL: c.LLMBaseURL,
		APIKey:  c.LLMAPIKey,
		Model:   c.LLMModel,
	}
	slog.Info("captured autonomous defaults before first credential change",
		"component", "device", "base_url", c.LLMBaseURL, "model", c.LLMModel)
}

func applyLLMFields(c *config.Config, data domain.UpdateConfigRequest, ch *updateChanges) {
	prevModel := c.LLMModel
	prevBaseURL := c.LLMBaseURL
	prevAPIKey := c.LLMAPIKey
	if data.LLMAPIKey != "" {
		c.LLMAPIKey = data.LLMAPIKey
	}
	if data.LLMBaseURL != "" {
		c.LLMBaseURL = urlnorm.NormalizeBaseURL(data.LLMBaseURL)
	}
	if data.LLMModel != "" {
		c.LLMModel = data.LLMModel
	}
	ch.model = data.LLMModel != "" && data.LLMModel != prevModel
	ch.baseURL = data.LLMBaseURL != "" && c.LLMBaseURL != prevBaseURL
	// PATCH semantics: an empty request field means "leave it alone", so a
	// re-save that only touched (say) llm_model must NOT count as an apiKey
	// change. Only flag a rotation when the request explicitly carried a new
	// key AND that key differs from what was on disk. Missing this flag was
	// the whole reason openclaw.json's apiKey drifted from config.json's after
	// a rotation — RefreshModelsConfig never fired.
	ch.apiKey = data.LLMAPIKey != "" && c.LLMAPIKey != prevAPIKey
	ch.newModel = c.LLMModel

	ch.thinking = data.LLMDisableThinking != nil
	if ch.thinking {
		c.LLMDisableThinking = data.LLMDisableThinking
	}
}

// applyVoicePipelineFields applies the STT/TTS/realtime cluster. PATCH
// semantics: empty = leave existing value alone. Stops the Settings page
// (which ships its full form body even when the operator only edited one tab)
// from wiping STT/TTS/Deepgram fields it never showed.
func applyVoicePipelineFields(c *config.Config, data domain.UpdateConfigRequest, ch *updateChanges) {
	if data.WakeWord != nil {
		c.WakeWord = data.WakeWord
	}
	if data.DeepgramAPIKey != "" {
		c.DeepgramAPIKey = data.DeepgramAPIKey
	}
	if data.STTAPIKey != "" {
		c.STTAPIKey = data.STTAPIKey
	}
	// Clear before set, so a request carrying both ends up holding the newly
	// typed key rather than nothing. The UI never sends both; this only fixes
	// the order if it ever does.
	if data.ClearTTSAPIKey {
		c.TTSAPIKey = ""
	}
	if data.TTSAPIKey != "" {
		c.TTSAPIKey = data.TTSAPIKey
	}
	if data.STTBaseURL != "" {
		c.STTBaseURL = urlnorm.NormalizeBaseURL(data.STTBaseURL)
	}
	if data.TTSBaseURL != "" {
		c.TTSBaseURL = urlnorm.NormalizeBaseURL(data.TTSBaseURL)
	}
	// Operators pick a language; the matching Deepgram SKU is auto-derived
	// because end users don't know which model handles which language.
	if data.STTLanguage != "" {
		c.STTLanguage = data.STTLanguage
		c.STTModel = sttModelForLanguage(data.STTLanguage)
	}
	ch.newLang = c.STTLanguage
	ch.lang = ch.prevLang != ch.newLang

	if data.TTSProvider != "" {
		c.TTSProvider = data.TTSProvider
	}
	if data.TTSVoice != "" {
		c.TTSVoice = data.TTSVoice
	}
	if data.TTSSpeed != nil {
		speed := *data.TTSSpeed
		c.TTSSpeed = &speed
	}
	// Realtime block (validated by the caller before the lock).
	if data.Realtime != nil {
		before := realtimeFingerprint(c)
		applyRealtimeSet(c, *data.Realtime)
		// Sent is not the same as changed. The settings page puts the realtime
		// block in *every* save, so treating its presence as a change restarted
		// hal on every save — including a voice-only one, which is precisely
		// what pushing TTS config live exists to avoid.
		ch.realtime = realtimeFingerprint(c) != before
	}
}

func applyNetworkFields(c *config.Config, data domain.UpdateConfigRequest, ch *updateChanges) {
	ch.wifi = data.SSID != "" && data.SSID != c.NetworkSSID
	if data.SSID != "" {
		c.NetworkSSID = data.SSID
	}
	if data.Password != "" {
		c.NetworkPassword = data.Password
	}
	// Capture for the WiFi goroutine (avoid reading config after lock release).
	ch.newSSID = c.NetworkSSID
	ch.newPassword = c.NetworkPassword
}

func applyChannelPatch(c *config.Config, data domain.UpdateConfigRequest) {
	if data.Channel != "" {
		c.Channel = data.Channel
	}
	switch c.Channel {
	case domain.ChannelSlack:
		if data.SlackBotToken != "" {
			c.SlackBotToken = data.SlackBotToken
		}
		if data.SlackAppToken != "" {
			c.SlackAppToken = data.SlackAppToken
		}
		if data.SlackUserID != "" {
			c.SlackUserID = data.SlackUserID
		}
	case domain.ChannelDiscord:
		if data.DiscordBotToken != "" {
			c.DiscordBotToken = data.DiscordBotToken
		}
		if data.DiscordGuildID != "" {
			c.DiscordGuildID = data.DiscordGuildID
		}
		if data.DiscordUserID != "" {
			c.DiscordUserID = data.DiscordUserID
		}
	case domain.ChannelWhatsapp:
		if data.WhatsappUserID != "" {
			c.WhatsappUserID = data.WhatsappUserID
		}
	case domain.ChannelIMessage:
		// Omitted plain fields preserve the saved channel; explicit empty
		// strings clear individual fields without erasing unrelated settings.
		if data.BluebubblesServerURL != nil {
			c.BluebubblesServerURL = *data.BluebubblesServerURL
		}
		if data.BluebubblesUserAddress != nil {
			c.BluebubblesUserAddress = *data.BluebubblesUserAddress
		}
		if data.BluebubblesPassword != "" {
			c.BluebubblesPassword = data.BluebubblesPassword
		}
		if data.BluebubblesCallerContext != nil {
			c.BluebubblesCallerContext = *data.BluebubblesCallerContext
		}
	default:
		if data.TelegramBotToken != "" {
			c.TelegramBotToken = data.TelegramBotToken
		}
		if data.TelegramUserID != "" {
			c.TelegramUserID = data.TelegramUserID
		}
	}
}

func applyMQTTFields(c *config.Config, data domain.UpdateConfigRequest) {
	if data.MQTTEndpoint != "" {
		c.MQTTEndpoint = data.MQTTEndpoint
	}
	if data.MQTTUsername != "" {
		c.MQTTUsername = data.MQTTUsername
	}
	if data.MQTTPassword != "" {
		c.MQTTPassword = data.MQTTPassword
	}
	if data.MQTTPort != 0 {
		c.MQTTPort = data.MQTTPort
	}
	if data.FAChannel != "" {
		c.FAChannel = data.FAChannel
	}
	if data.FDChannel != "" {
		c.FDChannel = data.FDChannel
	}
}

// UpdateConfig saves updated config fields. All fields are optional; empty strings are skipped.
// Side effects per field cluster: wifi → connect-wifi (wpa_supplicant reload),
// llm_model/thinking → openclaw, stt_language → openclaw NewSession + hal,
// voice-pipeline fields → hal. Other fields persist only; restart os-server for full effect.
func (s *Service) UpdateConfig(data domain.UpdateConfigRequest) error {
	if data.Environment != nil {
		if err := data.Environment.Validate(); err != nil {
			return err
		}
	}
	if err := domain.ValidateTTSSpeed(data.TTSSpeed); err != nil {
		return err
	}
	// bcrypt is CPU-intensive; compute before acquiring the config lock.
	var adminHash string
	if data.AdminPassword != "" {
		hash, err := bcrypt.GenerateFromPassword([]byte(data.AdminPassword), bcrypt.DefaultCost)
		if err != nil {
			return fmt.Errorf("hash admin password: %w", err)
		}
		adminHash = string(hash)
	}

	// Validate the realtime payload before the lock so an invalid request fails
	// without a partial save (same as bcrypt above).
	if data.Realtime != nil {
		if err := s.validateRealtimeSet(*data.Realtime); err != nil {
			return err
		}
	}

	// All field mutations happen inside WithLockSave so they are marshalled
	// atomically — the watcher goroutine's SetLLMModel cannot interleave with
	// a partial config snapshot.
	var ch updateChanges
	if err := s.config.WithLockSave(func(c *config.Config) {
		ch = applyUpdate(c, data, adminHash)
	}); err != nil {
		return fmt.Errorf("save config: %w", err)
	}
	slog.Info("config updated", "component", "device")
	s.fireConfigSideEffects(ch)
	return nil
}

// fireConfigSideEffects runs the per-cluster follow-ups after a successful
// UpdateConfig save. config.mu is already released, so the gateway calls below
// acquire their own locks without deadlock risk (consistent lock order).
func (s *Service) fireConfigSideEffects(ch updateChanges) {
	if ch.wifi {
		go func() {
			slog.Info("reconnecting to new WiFi", "component", "device", "ssid", ch.newSSID)
			if _, err := s.networkService.SetupNetwork(ch.newSSID, ch.newPassword); err != nil {
				slog.Error("WiFi reconnect failed", "component", "device", "error", err)
			}
		}()
	}
	// Channel/token edits via the Settings form only land in config.json, but the
	// gateway keeps messaging tokens in its OWN config (written by AddChannel:
	// `openclaw channels add` + plugin enable) and reads from there first. So a
	// save — even a gateway restart — won't apply them; re-run AddChannel to push
	// the change into the gateway. WhatsApp needs interactive QR pairing (MQTT
	// add_channel only), so it is excluded here.
	if ch.channel && ch.chanReq.Channel != domain.ChannelWhatsapp {
		go func() {
			if _, err := s.AddChannel(context.Background(), ch.chanReq); err != nil {
				slog.Error("apply channel change to gateway failed", "component", "device", "channel", ch.chanReq.Channel, "error", err)
			} else {
				slog.Info("channel change applied to gateway", "component", "device", "channel", ch.chanReq.Channel)
			}
		}()
	}
	s.syncLLMToGateway(ch)
	// When the operator switches stt_language explicitly, drop the in-session
	// chat history so the LLM doesn't keep replying in the previous language
	// out of inertia. SOUL.md tells it the latest turn wins, but a heavily
	// English/Vietnamese-biased history can still pull the next reply back —
	// a fresh session is the cleanest break.
	if ch.lang && s.agentGateway != nil {
		if key := s.agentGateway.GetSessionKey(); key != "" {
			go func() {
				if err := s.agentGateway.NewSession(key); err != nil {
					slog.Warn("openclaw NewSession on stt_language change failed", "component", "device", "error", err)
				} else {
					slog.Info("openclaw session reset for stt_language change", "component", "device", "from", ch.prevLang, "to", ch.newLang)
				}
			}()
		}
	}
	// Restart hal only when a field it reads at boot actually changed.
	// stt_language is covered by ch.lang (hal reads it via stt_language /
	// derived stt_model). Wifi/channel/MQTT/admin saves skip the restart.
	switch {
	case ch.halBoot || ch.lang || ch.realtime:
		s.restartHAL("voice config change")
	case ch.tts:
		// The common case, and the one that used to bounce hal for nothing.
		s.applyTTSConfig(s.config)
	}
}

// syncLLMToGateway pushes a model/thinking/baseURL change into the active
// gateway's own config. Sync primary model is the os-server → OpenClaw
// direction. When thinking or baseURL also changed, RefreshModelsConfig
// handles primary update + reasoning patch + baseUrl in a single write +
// restart — skip UpdatePrimaryModel to avoid a redundant gateway restart.
func (s *Service) syncLLMToGateway(ch updateChanges) {
	if s.agentGateway == nil {
		return
	}
	if ch.model && !ch.thinking && !ch.baseURL && !ch.apiKey {
		if err := s.agentGateway.UpdatePrimaryModel(ch.newModel); err != nil {
			if errors.Is(err, domain.ErrNotSupportedByRuntime) {
				// hermes/picoclaw cannot be patched in place, but their presync
				// reads llm_model out of the config.json just saved — so the
				// self-heal applies it now rather than at the next boot. Same
				// response as the baseURL/key branch below; the sentinel means
				// "not patchable", never "nothing to do".
				//
				// It used to only log. That was survivable while those runtimes
				// pinned a fixed model alias, but a device on a custom brain runs
				// the operator's model, and a model-only change sat unapplied
				// until something else happened to restart the gateway.
				slog.Info("primary model not device-patchable, running onboarding self-heal", "component", "device", "backend", s.agentGateway.Name())
				go func() {
					if err := s.agentGateway.EnsureOnboarding(); err != nil {
						slog.Warn("onboarding self-heal after model change failed", "component", "device", "error", err)
					}
				}()
			} else {
				slog.Warn("update primary model failed", "component", "device", "error", err)
			}
		}
	}
	if ch.thinking || ch.baseURL || ch.apiKey {
		// RefreshModelsConfig syncs agents.defaults.model.primary, per-model
		// reasoning, and providers.autonomous.baseUrl in one write + restart.
		if err := s.agentGateway.RefreshModelsConfig(); err != nil {
			if errors.Is(err, domain.ErrNotSupportedByRuntime) {
				// hermes/picoclaw can't be patched directly, but hermes's
				// EnsureOnboarding presync re-reads llm_base_url/llm_api_key from
				// the config.json we just saved — run it so a baseURL/key change
				// applies now instead of at the next boot. Idempotent on both
				// backends; restarts the gateway only when the config changed.
				slog.Info("models config not device-patchable, running onboarding self-heal", "component", "device", "backend", s.agentGateway.Name())
				go func() {
					if err := s.agentGateway.EnsureOnboarding(); err != nil {
						slog.Warn("onboarding self-heal after llm config change failed", "component", "device", "error", err)
					}
				}()
			} else {
				slog.Error("refresh models config failed", "component", "device", "error", err)
			}
		}
	}
}

// UpdateVoiceConfig updates only TTS provider/voice/speed and STT language — safe to call from MQTT
// handlers since it does not touch API keys, MQTT credentials, or WiFi config.
func (s *Service) UpdateVoiceConfig(provider, voice, language string, speed *float64) error {
	if err := domain.ValidateTTSSpeed(speed); err != nil {
		return err
	}
	prevLang := s.config.STTLanguage
	if provider != "" {
		s.config.TTSProvider = provider
	}
	if voice != "" {
		s.config.TTSVoice = voice
	}
	if language != "" {
		s.config.STTLanguage = language
		s.config.STTModel = sttModelForLanguage(language)
	}
	if speed != nil {
		value := *speed
		s.config.TTSSpeed = &value
	}
	if err := s.config.Save(); err != nil {
		return fmt.Errorf("save config: %w", err)
	}
	slog.Info("voice config updated", "component", "device", "provider", s.config.TTSProvider, "voice", s.config.TTSVoice, "language", s.config.STTLanguage)
	if language != "" && prevLang != s.config.STTLanguage && s.agentGateway != nil {
		if key := s.agentGateway.GetSessionKey(); key != "" {
			go func() {
				if err := s.agentGateway.NewSession(key); err != nil {
					slog.Warn("NewSession on language change failed", "component", "device", "error", err)
				}
			}()
		}
	}
	if language != "" && prevLang != s.config.STTLanguage {
		// stt_language is read at boot; nothing can be pushed for it.
		s.restartHAL("stt language change")
	} else {
		s.applyTTSConfig(s.config)
	}
	return nil
}

// sttModelForLanguage maps a BCP-47 language code to the Deepgram SKU exposed
// by the Autonomous STT proxy. Empty input → empty model so hal falls back
// to its built-in default (flux-general-en). English stays on Flux (Deepgram's
// conversational EN model — EN-only). Everything else rides on Nova-3:
// Vietnamese since Jan 2026, Mandarin (zh/zh-CN/zh-Hans/zh-TW/zh-Hant) since
// Deepgram's 2026-03-31 release — zh was pinned to Nova-2 before that.
func sttModelForLanguage(lang string) string {
	switch lang {
	case "":
		return ""
	case i18n.LangEN:
		return "flux-general-en"
	default:
		return "nova-3-general"
	}
}

// RestoreAutonomousDefaults puts one section back on the credentials the device
// shipped with. Sections take different slices of the same stored set, because
// that is how they started out: the AI Brain uses url + key + model, Realtime
// and the voice pipeline use url + key.
//
// Implemented as an ordinary config update rather than a direct write, so it
// inherits every side effect a manual edit gets — hal restart or live TTS push,
// gateway model sync, agent session reset. A hand-rolled save would drift from
// that list the first time someone adds to it.
func (s *Service) RestoreAutonomousDefaults(section string) error {
	d := s.config.AutonomousDefaults
	if d == nil {
		return errors.New("no autonomous defaults stored on this device")
	}

	var req domain.UpdateConfigRequest
	switch strings.ToLower(strings.TrimSpace(section)) {
	case "llm":
		req.LLMAPIKey, req.LLMBaseURL, req.LLMModel = d.APIKey, d.BaseURL, d.Model
	case "voice":
		req.TTSAPIKey, req.TTSBaseURL = d.APIKey, d.BaseURL
		req.STTAPIKey, req.STTBaseURL = d.APIKey, d.BaseURL
	case "realtime":
		req.Realtime = &domain.RealtimeSetData{APIKey: d.APIKey, BaseURL: d.BaseURL}
	default:
		return fmt.Errorf("unknown section %q (want llm, voice or realtime)", section)
	}

	slog.Info("restoring autonomous defaults", "component", "device", "section", section)
	return s.UpdateConfig(req)
}

func autonomousDefaultBaseURL(c *config.Config) string {
	if c.AutonomousDefaults == nil {
		return ""
	}
	return c.AutonomousDefaults.BaseURL
}

func autonomousDefaultModel(c *config.Config) string {
	if c.AutonomousDefaults == nil {
		return ""
	}
	return c.AutonomousDefaults.Model
}
