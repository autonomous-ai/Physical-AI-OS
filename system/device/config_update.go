package device

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"golang.org/x/crypto/bcrypt"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/i18n"
	"go.autonomous.ai/os/system/lib/urlnorm"
	"go.autonomous.ai/os/system/server/config"
)

// GetPublicConfig returns the config with secrets replaced by `Has*` presence
// flags, so plaintext tokens never reach the browser.
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
		LLMConfigMode:            s.config.LLMMode(),
		Environment:              s.config.EnvironmentSettings(),
		Channel:                  s.config.Channel,
		TelegramUserID:           s.config.TelegramUserID,
		SlackUserID:              s.config.SlackUserID,
		DiscordGuildID:           s.config.DiscordGuildID,
		DiscordUserID:            s.config.DiscordUserID,
		WhatsappUserID:           s.config.WhatsappUserID,
		BluebubblesServerURL:     s.config.BluebubblesServerURL,
		BluebubblesUserAddress:   s.config.BluebubblesUserAddress,
		BluebubblesCallerContext: s.config.BluebubblesCallerContext,
		LLMModel:                 s.config.LLMModel,
		LLMBaseURL:               s.config.LLMBaseURL,
		LLMDisableThinking:       disableThinking,
		STTBaseURL:               s.config.STTBaseURL,
		TTSBaseURL:               s.config.TTSBaseURL,
		STTLanguage:              s.config.STTLanguage,
		STTModel:                 s.config.STTModel,
		TTSProvider:              s.config.TTSProvider,
		TTSVoice:                 s.config.TTSVoice,
		TTSSpeed:                 s.config.GetTTSSpeed(),
		WakeWord:                 s.config.WakeWordEnabled(),
		AgentName:                agentName,
		WakePhrases:              i18n.BuildSupportedVoiceWakeWords(agentName, deviceType),
		DeviceID:                 deviceID,
		Mac:                      GetDeviceMac(),
		NetworkSSID:              s.config.NetworkSSID,
		MQTTEndpoint:             s.config.MQTTEndpoint,
		MQTTUsername:             s.config.MQTTUsername,
		MQTTPort:                 s.config.MQTTPort,
		FAChannel:                s.config.FAChannel,
		FDChannel:                s.config.FDChannel,

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
		Realtime:                 s.RealtimePublic(),
	}
}

// RealtimePublic is the realtime config for read-back (web and MQTT); the key appears only as HasAPIKey.
func (s *Service) RealtimePublic() domain.RealtimePublic {
	return domain.RealtimePublic{
		Enabled:   s.config.RealtimeEnabled(),
		Provider:  s.config.RealtimeProvider(),
		Model:     s.config.RealtimeModel(),
		Voice:     s.config.RealtimeVoice(),
		Reasoning: s.config.RealtimeReasoning(),
		// Only the explicit override: re-persisting a derived URL breaks /ws/gemini.
		BaseURL:   s.config.RealtimeBaseURLOverride(),
		HasAPIKey: s.config.RealtimeHasAPIKey(),
		WebSearch: s.config.RealtimeWebSearch(),
	}
}

// VerifyAdminPassword returns nil when password matches the stored bcrypt hash.
// Callers must not surface the specific error to clients.
func (s *Service) VerifyAdminPassword(password string) error {
	if s.config.AdminPasswordHash == "" {
		return fmt.Errorf("admin password not configured")
	}
	return bcrypt.CompareHashAndPassword([]byte(s.config.AdminPasswordHash), []byte(password))
}

// updateChanges records which field clusters a save changed plus post-save
// values; computed inside WithLockSave so it never sees a torn snapshot.
type updateChanges struct {
	llmMode  bool // explicit ownership apply, including retries of the same choice
	model    bool // llm_model changed → sync primary into the gateway
	thinking bool // llm_disable_thinking changed → RefreshModelsConfig
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

// bootSnapshot is the comparable set of fields HAL reads only at boot; a
// before/after difference gates the HAL restart.
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

// ttsSnapshot is the TTS config HAL accepts live (POST /voice/tts/config),
// avoiding a 10-15s deaf restart.
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

// realtimeFingerprint renders the realtime block as JSON for before/after comparison.
func realtimeFingerprint(c *config.Config) string {
	if c.Realtime == nil {
		return ""
	}
	b, err := json.Marshal(c.Realtime)
	if err != nil {
		return ""
	}
	return string(b)
}

// channelSnapshot is the channel identity + tokens; a difference means the
// change must be re-pushed into the gateway's own config.
type channelSnapshot struct {
	channel                  string
	telegramBotToken         string
	telegramUserID           string
	slackBotToken            string
	slackAppToken            string
	slackUserID              string
	discordBotToken          string
	discordGuildID           string
	discordUserID            string
	bluebubblesServerURL     string
	bluebubblesPassword      string
	bluebubblesUserAddress   string
	bluebubblesCallerContext string
}

func channelFields(c *config.Config) channelSnapshot {
	return channelSnapshot{
		channel:                  c.Channel,
		telegramBotToken:         c.TelegramBotToken,
		telegramUserID:           c.TelegramUserID,
		slackBotToken:            c.SlackBotToken,
		slackAppToken:            c.SlackAppToken,
		slackUserID:              c.SlackUserID,
		discordBotToken:          c.DiscordBotToken,
		discordGuildID:           c.DiscordGuildID,
		discordUserID:            c.DiscordUserID,
		bluebubblesServerURL:     c.BluebubblesServerURL,
		bluebubblesPassword:      c.BluebubblesPassword,
		bluebubblesUserAddress:   c.BluebubblesUserAddress,
		bluebubblesCallerContext: c.BluebubblesCallerContext,
	}
}

// applyUpdate mutates c per the PATCH request and reports what changed (no I/O).
// Must run inside WithLockSave; adminHash is precomputed outside the lock.
func applyUpdate(c *config.Config, data domain.UpdateConfigRequest, adminHash string) updateChanges {
	var ch updateChanges
	prevBoot := bootFields(c)
	prevTTS := ttsFields(c)
	prevWakeWord := c.WakeWordEnabled()
	prevChannel := channelFields(c)
	ch.prevLang = c.STTLanguage

	// Must run before any field is overwritten.
	if data.LLMConfigMode != nil || requestChangesCredentials(data) {
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

	if adminHash != "" {
		c.AdminPasswordHash = adminHash
	}

	ch.halBoot = bootFields(c) != prevBoot || c.WakeWordEnabled() != prevWakeWord
	ch.tts = ttsFields(c) != prevTTS
	ch.channel = channelFields(c) != prevChannel
	// Use full post-save values, not just the delta.
	ch.chanReq = domain.AddChannelRequest{
		Channel:          c.Channel,
		TelegramBotToken: c.TelegramBotToken, TelegramUserID: c.TelegramUserID,
		SlackBotToken: c.SlackBotToken, SlackAppToken: c.SlackAppToken, SlackUserID: c.SlackUserID,
		DiscordBotToken: c.DiscordBotToken, DiscordGuildID: c.DiscordGuildID, DiscordUserID: c.DiscordUserID,
		BluebubblesServerURL:     c.BluebubblesServerURL,
		BluebubblesPassword:      c.BluebubblesPassword,
		BluebubblesUserAddress:   c.BluebubblesUserAddress,
		BluebubblesCallerContext: c.BluebubblesCallerContext,
	}
	return ch
}

// requestChangesCredentials reports whether the save carries a credential key,
// URL or model.
func requestChangesCredentials(d domain.UpdateConfigRequest) bool {
	if d.LLMAPIKey != "" || d.LLMBaseURL != "" || d.LLMModel != "" ||
		d.TTSAPIKey != "" || d.TTSBaseURL != "" ||
		d.STTAPIKey != "" || d.STTBaseURL != "" {
		return true
	}
	return d.Realtime != nil && (d.Realtime.APIKey != "" || d.Realtime.BaseURL != "")
}

// captureAutonomousDefaults snapshots the shipped credentials before the first
// edit. Runs at most once (re-capturing would lose the real defaults) and skips
// an empty set.
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
	if data.LLMConfigMode != nil {
		c.LLMConfigMode = *data.LLMConfigMode
		ch.llmMode = true
	}
	// Preserve the OS credentials used by voice/backend services and by a later
	// switch back to OS management. Stale Settings payloads must not replace them.
	if c.LLMConfigMode == "runtime" {
		return
	}

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
	// Rotation only when a non-empty key differs from disk (PATCH semantics).
	ch.apiKey = data.LLMAPIKey != "" && c.LLMAPIKey != prevAPIKey
	ch.newModel = c.LLMModel

	ch.thinking = data.LLMDisableThinking != nil && *data.LLMDisableThinking != c.LLMThinkingDisabled()
	if data.LLMDisableThinking != nil {
		c.LLMDisableThinking = data.LLMDisableThinking
	}
}

// applyVoicePipelineFields applies the STT/TTS/realtime cluster (empty = keep).
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
	// Clear before set so a new key wins when both are sent.
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
	if data.Realtime != nil {
		before := realtimeFingerprint(c)
		applyRealtimeSet(c, *data.Realtime)
		// The block is sent on every save; only a real change restarts HAL.
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
	// Captured for the WiFi goroutine, which runs after lock release.
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

// UpdateConfig saves non-empty fields (PATCH) and fires per-cluster side effects
// (wifi, gateway, channel, HAL).
func (s *Service) UpdateConfig(data domain.UpdateConfigRequest) error {
	if data.LLMConfigMode != nil && *data.LLMConfigMode != "os" && *data.LLMConfigMode != "runtime" {
		return fmt.Errorf("invalid llm_config_mode %q (want os or runtime)", *data.LLMConfigMode)
	}
	// Config apply and runtime switching both write native configuration.
	if !s.runtimeSwitchMu.TryLock() {
		return ErrAgentRuntimeSwitchInProgress
	}
	defer s.runtimeSwitchMu.Unlock()

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

	// Validate before the lock so an invalid request never partially saves.
	if data.Realtime != nil {
		if err := s.validateRealtimeSet(*data.Realtime); err != nil {
			return err
		}
	}

	// Serialize with MQTT wake updates through persistence and HAL apply.
	s.wakeApply.mu.Lock()

	// Mutate inside WithLockSave so the model watcher cannot interleave.
	var ch updateChanges
	if err := s.config.WithLockSave(func(c *config.Config) {
		previousWake := c.WakeWordEnabled()
		ch = applyUpdate(c, data, adminHash)
		s.wakeApply.pending = s.wakeApply.pending || c.WakeWordEnabled() != previousWake
	}); err != nil {
		s.wakeApply.mu.Unlock()
		return fmt.Errorf("save config: %w", err)
	}
	slog.Info("config updated", "component", "device")
	wakeApply := s.wakeApply.pending
	var wakeErr error
	if wakeApply {
		wakeErr = s.applyPendingWakeWord()
	}
	s.wakeApply.mu.Unlock()
	// A wake restart covers the whole saved HAL config. Do not enqueue a second
	// restart/live TTS update, including after failure: the wake retry owns it.
	// Other saved fields still need their side effects even if HAL apply failed.
	var modeErr error
	if ch.llmMode {
		modeErr = s.applyLLMMode()
	}
	s.fireConfigSideEffects(ch, wakeApply)
	return errors.Join(wakeErr, modeErr)
}

// fireConfigSideEffects runs per-cluster follow-ups after a save. config.mu must
// already be released (gateway calls take their own locks).
func (s *Service) fireConfigSideEffects(ch updateChanges, halHandled bool) {
	// The gateway keeps tokens in its own config, so re-run AddChannel.
	// WhatsApp is excluded: it needs interactive QR pairing.
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
	// New session so history does not keep the previous language.
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
	// Restart HAL only when a boot-read field changed; TTS is pushed live.
	switch {
	case halHandled:
		// The wake path already attempted this saved HAL configuration.
	case ch.halBoot || ch.lang || ch.realtime:
		s.restartHAL("voice config change")
	case ch.tts:
		s.applyTTSConfig(s.config)
	}
	// Schedule only after synchronous side effects finish, so their latency
	// cannot consume the response grace period before the HTTP handler returns.
	if ch.wifi {
		scheduleConfigWiFiReconnect(ch, s.networkService.SetupNetwork)
	}
}

// Give the Settings response time to reach clients before AP/STA teardown.
// A successful save acknowledges persistence; association still happens later.
const configWiFiResponseGrace = 2 * time.Second

func scheduleConfigWiFiReconnect(ch updateChanges, reconnect func(string, string) (bool, error)) *time.Timer {
	if !ch.wifi {
		return nil
	}
	return time.AfterFunc(configWiFiResponseGrace, func() {
		slog.Info("reconnecting to new WiFi", "component", "device", "ssid", ch.newSSID)
		if _, err := reconnect(ch.newSSID, ch.newPassword); err != nil {
			slog.Error("WiFi reconnect failed", "component", "device", "error", err)
		}
	})
}

// syncLLMToGateway pushes model/thinking/baseURL changes into the gateway's
// config; RefreshModelsConfig covers the model too, avoiding a second restart.
func (s *Service) syncLLMToGateway(ch updateChanges) {
	if ch.llmMode || s.config.LLMRuntimeManaged() || s.agentGateway == nil {
		return
	}
	if ch.model && !ch.thinking && !ch.baseURL && !ch.apiKey {
		if err := s.agentGateway.UpdatePrimaryModel(ch.newModel); err != nil {
			if errors.Is(err, domain.ErrNotSupportedByRuntime) {
				// Not patchable in place: the onboarding presync re-reads config.json.
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
		if err := s.agentGateway.RefreshModelsConfig(); err != nil {
			if errors.Is(err, domain.ErrNotSupportedByRuntime) {
				// Not patchable in place: the onboarding presync re-reads config.json.
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

// applyLLMMode reapplies even unchanged OS values and refreshes process env.
// Persistence happens first; an apply failure is returned so the same selection
// can be retried without claiming that the runtime switched successfully.
func (s *Service) applyLLMMode() error {
	if s.agentGateway == nil {
		return nil
	}
	if !s.config.LLMRuntimeManaged() {
		err := s.agentGateway.RefreshModelsConfig()
		if err == nil {
			return nil
		}
		if !errors.Is(err, domain.ErrNotSupportedByRuntime) {
			return fmt.Errorf("LLM mode saved; apply runtime config: %w", err)
		}
	}
	if err := s.agentGateway.EnsureOnboarding(); err != nil {
		return fmt.Errorf("LLM mode saved; apply runtime config: %w", err)
	}
	if err := s.agentGateway.RestartAgent(); err != nil {
		return fmt.Errorf("LLM mode saved; restart runtime: %w", err)
	}
	return nil
}

// UpdateVoiceConfig updates only TTS provider/voice/speed and STT language.
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

// sttModelForLanguage maps a BCP-47 code to a Deepgram SKU: English uses Flux,
// others Nova-3; empty input lets HAL use its default.
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

// RestoreAutonomousDefaults restores one section to the shipped credentials via
// a normal config update, so it inherits every side effect.
func (s *Service) RestoreAutonomousDefaults(section string) error {
	d := s.config.AutonomousDefaults
	if d == nil {
		return errors.New("no autonomous defaults stored on this device")
	}

	var req domain.UpdateConfigRequest
	switch strings.ToLower(strings.TrimSpace(section)) {
	case "llm":
		mode := "os"
		req.LLMConfigMode = &mode
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
